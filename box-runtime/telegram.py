"""telegram.py — owner-only Telegram bot via long polling (no webhook; Plan B has no public URL). STEP 9.

Only updates from owner_chat_id are handled (messages and inline-button callbacks); everything else is ignored.
Commands work as /command or plain text; the command layer is commands.py (shared with the CLI / Grok Bot app).
Pushes come from bus "notify" exactly as the agents wrote them (PAPER pushes already start with "PAPER · ").
Inline callbacks carry "<cmd>:<mint>" -> bus "control" {cmd, mint} (Executor / Risk subscribe).
The bot token lives in the URL, so request errors are logged by type only.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

import commands as C
from config import all_secret_values, read_secret
import notifier

log = logging.getLogger("desk.telegram")
API = "https://api.telegram.org/bot{token}/{method}"

HELP = C.HELP


def _chat_id(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if msg:
        return (msg.get("chat") or {}).get("id")
    cq = update.get("callback_query")
    if cq:
        return ((cq.get("message") or {}).get("chat") or {}).get("id") or (cq.get("from") or {}).get("id")
    return None


def is_owner(update: dict, owner_chat_id) -> bool:
    try:
        cid = _chat_id(update)
        return owner_chat_id is not None and cid is not None and int(cid) == int(owner_chat_id)
    except (TypeError, ValueError):
        return False


def redact(text: str) -> str:
    for s in all_secret_values():
        text = text.replace(s, "***")
    return text


def keyboard(buttons, mint: str | None = None) -> dict | None:
    """notify/Reply buttons (flat row or list of rows) -> Telegram inline keyboard.
    URL buttons must come from links.py via notifier (r=savipww). Callbacks get ":mint" so a tap knows its coin."""
    if not buttons:
        return None
    rows_in = buttons if isinstance(buttons[0], list) else [buttons]
    rows = []
    for r in rows_in:
        row = []
        for b in r or []:
            if b.get("url"):
                row.append({"text": b["text"], "url": b["url"]})
            elif b.get("callback"):
                cb = b["callback"] if (":" in b["callback"] or not mint) else f"{b['callback']}:{mint}"
                row.append({"text": b["text"], "callback_data": cb[:64]})
        if row:
            rows.append(row)
    return {"inline_keyboard": rows} if rows else None


class Telegram:
    name = "telegram"
    keyboard = staticmethod(keyboard)

    def __init__(self, ctx):
        self.ctx, self.cfg, self.db, self.bus = ctx, ctx.cfg, ctx.db, ctx.bus
        self.token = read_secret("TG_TOKEN")
        self.owner = self.cfg.get("owner_chat_id")
        self.offset = None
        self.notify_q = self.bus.subscribe("notify")
        self.cmds = C.Commands(ctx, who="telegram")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10))

    async def call(self, method, **params):
        try:
            # hard cap: httpx timeouts are per read, a trickling/half-open socket could otherwise hang forever
            r = await asyncio.wait_for(
                self.http.post(API.format(token=self.token, method=method), json=params), timeout=60)
            data = r.json()
            return data.get("result") if data.get("ok") else None
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            log.warning("telegram %s failed: %s", method, type(e).__name__)
            return None

    async def send(self, text: str, buttons=None, mint: str | None = None):
        if not (self.token and self.owner):
            return None
        params = dict(chat_id=int(self.owner), text=redact(text)[:4000], disable_web_page_preview=True)
        kb = keyboard(buttons, mint)
        if kb:
            params["reply_markup"] = kb
        return await self.call("sendMessage", **params)

    # ---- commands (plain text or /command)
    async def handle(self, text: str):
        try:
            replies = await self.cmds.handle(text)
        except Exception as e:  # never kill the poll loop on a bad command
            log.exception("command failed")
            replies = [C.Reply(f"That failed: {type(e).__name__}. Try again or send help.")]
        for r in replies:
            await self.send(r.text, r.buttons, r.mint)

    async def on_callback(self, cq: dict):
        ack = self.cmds.callback(cq.get("data") or "", cq.get("message"))
        await self.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=ack[:190])

    async def dispatch(self, u: dict):
        if not is_owner(u, self.owner):
            return  # owner-only
        if u.get("callback_query"):
            await self.on_callback(u["callback_query"])
            return
        text = (u.get("message") or {}).get("text") or ""
        if text.strip():
            await self.handle(text)

    async def poll(self):
        while True:
            params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if self.offset is not None:
                params["offset"] = self.offset
            res = await self.call("getUpdates", **params)
            if res is None:
                self.db.beat("telegram", "degraded", f"getUpdates failed; retry {time.strftime('%H:%M:%S')}")
                await asyncio.sleep(5)
                continue
            for u in res:
                self.offset = u["update_id"] + 1
                await self.dispatch(u)
            self.db.beat("telegram", "ok", f"polling; last {time.strftime('%H:%M:%S')}")

    async def notifier(self):
        while True:
            m = await self.notify_q.get()
            text = notifier.with_url_fallback(m["text"], m.get("mint"))
            C.record_push(self.db, {**m, "text": text})  # "buy X" resends tonight's push as sent
            for attempt in range(3):  # STEP 5: Telegram error -> retry 3 times, then log "push failed"
                if await self.send(text, m.get("buttons"), m.get("mint")) is not None:
                    break
                await asyncio.sleep(2)
            else:
                log.warning("push failed (%s)", m.get("kind") or "notify")

    async def guarded(self):
        if not self.token or not self.owner:
            while True:  # keep beating so health.py doesn't treat a disabled bot as hung
                self.db.beat("telegram", "disabled", "no TG_TOKEN or owner_chat_id")
                await asyncio.sleep(30)
        self.db.beat("telegram", "ok", "starting")
        if getattr(self, "_announced", False):  # in-process restart: don't re-announce
            await asyncio.gather(self.poll(), self.notifier())
            return
        self._announced = True
        await self.send(f"🕷 Desk online ({self.cfg['desk']['mode']}). Send help for commands.")
        await asyncio.gather(self.poll(), self.notifier())
