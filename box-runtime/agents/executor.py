"""AGENT 4 · EXECUTOR — never picks a coin. Does arithmetic and sends 1 button. Never places a FOMO order, never touches funds.

Input: bus "signal" (Analyst JSON, passed) + settings + open positions + a fresh DexScreener read.
SIZE  size = stack_pct% of stack, capped at max_pool_pct% of pool liquidity; under min_ticket: bump (if free covers) | skip.
RE-CHECK  liquidity down > recheck_liq_drop_pct since Scanner, price up > recheck_price_up_pct since Analyst -> skip.
PUSH  1 per coin per night, [Buy on FOMO] = links.coin_url(mint, "solana") only, [Bought] [Skip] callbacks.
FILL  db.signals (price = priceUsd at push time). PAPER: fill at the signal price at once -> positions + Activity
      "Bought X / Executor · {time}" -> bus "fill" (Risk owns the exit); cash -= ticket + fees.py buy side.
      LIVE (STEP 11): nothing fills on the push. Only the owner's [Bought] tap opens the position (at the signal
      price, no PAPER cash touched); no tap in confirm_window_min = "missed", no position; [Skip] -> db.skips
      until the morning report. Money only moves when the owner buys inside FOMO.
"""
from __future__ import annotations

import asyncio
import time

import fees
import links
import notifier
from agents.common import Agent, agent_paused, iso, short, store_price
from db import next_report, night_start


def ticket_check(stack: float, cfg_e: dict) -> tuple[float, str | None]:
    """STEP 10 ticket check -> (min_stack, message or None)."""
    min_ticket, pct = float(cfg_e["min_ticket_usd"]), float(cfg_e["stack_pct"])
    min_stack = min_ticket / (pct / 100)
    if stack >= min_stack:
        return min_stack, None
    if cfg_e.get("below_min", "bump") == "bump":
        p = min_ticket / stack * 100 if stack else 0
        return min_stack, f"tickets bumped to ${min_ticket:.2f} = {p:.1f}% of stack"
    return min_stack, f"every coin will be skipped, stack under ${min_stack:,.2f}"


SKIP_ASK = "Switch to bump (set below_min to bump) or raise the stack."


def ticket_text(msg: str, tag: str = "", icon: bool = False) -> str:
    """The owner-facing line: bump -> as is; skip -> warning + the STEP 10 ask (switch to bump or raise the stack).
    Pushes keep "PAPER · " first (STEP 9); chat replies may lead with the warning sign (icon=True)."""
    if "skipped" in msg:
        return f"{'⚠️ ' if icon else ''}{tag}{msg}. {SKIP_ASK}"
    return f"{tag}{msg}"


def size_ticket(stack: float, free: float, pool_liq: float | None, cfg_e: dict) -> dict:
    """-> {raw, size, cap, skip}. cap = which rule set the size (stack_pct | pool | bump)."""
    raw = stack * float(cfg_e["stack_pct"]) / 100
    size, cap = raw, "stack_pct"
    if pool_liq is not None:
        pool_cap = pool_liq * float(cfg_e["max_pool_pct"]) / 100
        if size > pool_cap:
            size, cap = pool_cap, "pool"
    min_ticket = float(cfg_e["min_ticket_usd"])
    if size < min_ticket:
        if cfg_e.get("below_min", "bump") == "bump":
            size, cap = min_ticket, "bump"
        else:
            return {"raw": round(raw, 2), "size": None, "cap": cap, "skip": f"under ${min_ticket:.2f}"}
    if size > free + 1e-9:
        return {"raw": round(raw, 2), "size": None, "cap": cap, "skip": "no free stack"}
    return {"raw": round(raw, 2), "size": round(size, 2), "cap": cap, "skip": None}


def recheck(scan_liq, liq_now, analyst_price, price_now, cfg_e) -> str | None:
    if scan_liq and liq_now is not None and liq_now < scan_liq * (1 - cfg_e["recheck_liq_drop_pct"] / 100):
        return "liquidity dropping"
    if analyst_price and price_now and price_now > analyst_price * (1 + cfg_e["recheck_price_up_pct"] / 100):
        return "ran before the push"
    return None


def push_text(paper: bool, name, size, pct, score, stop_pct, trail_pct) -> str:
    return (f"{'PAPER · ' if paper else ''}Buy {name} · ${size:,.2f} · {pct:g}% of stack\n"
            f"Auditor 8/8 · Analyst {score} · stop -{stop_pct:g}% · trail {trail_pct:g}%")


class Executor(Agent):
    name = "executor"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.inbox = self.bus.subscribe("signal")
        self.control = self.bus.subscribe("control")
        self.db.seed_paper(float(self.cfg["desk"]["paper_start_stack"]))
        self.pending: dict[str, dict] = {}  # LIVE: mint -> {signal_id, price, size, deadline}
        self.trades = 0

    @property
    def paper(self) -> bool:
        return str(self.cfg["desk"].get("mode", "PAPER")).upper() != "LIVE"

    def stack_free(self) -> tuple[float, float]:
        """PAPER: stack = simulated balance (cash + money in open positions); free = stack - open = cash.
        LIVE: stack = the owner's "stack N" setting (nothing can read the FOMO balance)."""
        if self.paper:
            cash = self.db.cash_usd()
            return cash + self.db.open_cost_usd("PAPER"), cash
        stack = float(self.cfg.get("live_stack") or 0)
        return stack, max(0.0, stack - self.db.open_cost_usd("LIVE"))

    def night_check(self) -> str | None:
        """STEP 10/11: ticket check at the start of every night (process start + each report_time rollover),
        before the first coin. Never silent: the warning goes to the notifier, the Executor card, the decision
        log and settings._ticket_check (Account tab). Skip + under min_stack does not start an empty night quietly."""
        stack, _ = self.stack_free()
        min_stack, msg = ticket_check(stack, self.my)
        self.db.set_setting("_ticket_check", {"ts": time.time(), "stack": round(stack, 2),
                                              "min_stack": round(min_stack, 2), "below_min": self.my.get("below_min"),
                                              "msg": msg})
        if msg:
            text = ticket_text(msg, "PAPER · " if self.paper else "")
            self.bus.publish("notify", {"text": text, "kind": "ticket_check"})
            self.log.warning("ticket check: %s", msg)
            self.dlog.info("night start stack=%.2f min_stack=%.2f below_min=%s ticket_check=%s", stack, min_stack,
                           self.my.get("below_min"), msg)
            if "skipped" in msg:
                self.beat("degraded", f"every coin skipped · stack under ${min_stack:,.2f}")
        return msg

    def handled_tonight(self, mint) -> bool:
        ns = night_start(report_time=self.cfg["desk"]["report_time"])
        return self.db.one("SELECT 1 AS e FROM signals WHERE mint=? AND ts>=?", (mint, ns)) is not None

    def out(self, mint, price, size, pct, mode, status, reason=None):
        return {"agent": "executor", "mint": mint, "signalPrice": price, "sizeUsd": size, "stackPct": pct,
                "link": links.coin_url(mint, "solana"), "mode": mode, "status": status, "reason": reason}

    def skip(self, mint, name, price, size, pct, reason, sz=None, stack=None):
        mode = "PAPER" if self.paper else "LIVE"
        o = self.out(mint, price, size, pct, mode, "skipped", reason)
        self.db.add_signal(mint, price, size, pct, o["link"], mode, "skipped")
        self.activity("rejected", mint, f"Rejected {name}", f"Executor · {reason}")
        self.dlog.info("%s stack=%s raw=%s cap=%s final=%s mode=%s status=skipped(%s) link=%s", mint, stack,
                       (sz or {}).get("raw"), (sz or {}).get("cap"), size, mode, reason, o["link"])
        return o

    async def handle(self, sig: dict, market: dict | None = None) -> dict | None:
        mint = sig["mint"]
        if self.handled_tonight(mint) or self.db.open_position(mint) or mint in self.pending:
            self.log.info("ignore %s: already pushed tonight / open", mint)
            return None
        ctx = sig.get("ctx") or {}
        name = ctx.get("name") or self.coin_name(mint)
        if self.ctx.state.get("paused"):
            return self.skip(mint, name, None, None, None, "paused")
        if agent_paused(self.ctx, "risk"):  # nobody would guard the exit
            return self.skip(mint, name, None, None, None, "risk paused")
        if market is None:
            market = await self.ctx.src.market(mint)
        store_price(self.db, market, mint)
        price = (market or {}).get("price_usd")
        if not price:
            return self.skip(mint, name, None, None, None, "no price")
        why = recheck((ctx.get("scanner") or {}).get("liquidityUsd"), market.get("liquidity_usd"),
                      ctx.get("priceUsd"), price, self.my)
        if why:
            return self.skip(mint, name, price, None, None, why)
        stack, free = self.stack_free()
        sz = size_ticket(stack, free, market.get("liquidity_usd"), self.my)
        if sz["skip"]:
            return self.skip(mint, name, price, None, None, sz["skip"], sz, stack)
        size = sz["size"]
        pct = round(size / stack * 100, 1) if stack else 0
        mode = "PAPER" if self.paper else "LIVE"
        link = links.coin_url(mint, "solana")
        sid = self.db.add_signal(mint, price, size, pct, link, mode, "sent")
        r = self.cfg["risk"]
        self.bus.publish("notify", {
            "text": push_text(self.paper, name, size, pct, sig.get("score"), r["stop_pct"], r["trail_pct"]),
            "mint": mint, "kind": "buy",
            "buttons": notifier.buy_buttons(mint),
        })
        if self.paper:  # PAPER: fill at the signal price at once (even if the push failed)
            o = self.fill(mint, name, sid, price, size, pct)
        else:
            self.pending[mint] = {"sid": sid, "price": price, "size": size, "pct": pct, "name": name,
                                  "deadline": time.time() + self.my["confirm_window_min"] * 60}
            o = self.out(mint, price, size, pct, mode, "sent")
        self.dlog.info("%s stack=%.2f raw=%s cap=%s final=%.2f mode=%s status=%s link=%s", mint, stack, sz["raw"],
                       sz["cap"], size, mode, o["status"], link)
        return o

    def fill(self, mint, name, sid, price, size, pct) -> dict:
        mode = "PAPER" if self.paper else "LIVE"
        stop = price * (1 - self.cfg["risk"]["stop_pct"] / 100)
        buy_fee = fees.side_fee_usd(size, self.cfg["desk"]["fees_ref_discount"])
        cur = self.db.x("INSERT INTO positions(mint,opened_at,entry_price,size_usd,peak_price,stop_price,trail_active,"
                        "fees_usd,mode) VALUES(?,?,?,?,?,?,0,?,?)",
                        (mint, time.time(), price, size, price, stop, round(buy_fee, 4), mode))
        self.db.x("UPDATE signals SET status='bought' WHERE id=?", (sid,))
        if mode == "PAPER":  # simulated balance: ticket + buy fee leave cash; equity = cash + open value
            self.db.set_cash_usd(self.db.cash_usd() - size - buy_fee)
            self.db.add_balance(self.db.equity_usd())
        self.trades += 1
        self.activity("bought", mint, f"Bought {name}", f"Executor · {time.strftime('%H:%M')}", size)
        o = self.out(mint, price, size, pct, mode, "bought")
        self.bus.publish("fill", {**o, "positionId": cur.lastrowid, "name": name})
        self.beat("ok", f"{self.trades} trades · ${size:,.2f} · {pct:g}% of stack")
        return o

    def on_control(self, c: dict):
        cmd, mint = c.get("cmd"), c.get("mint")
        p = self.pending.get(mint)
        if cmd == "bought" and p:
            self.pending.pop(mint)
            self.fill(mint, p["name"], p["sid"], p["price"], p["size"], p["pct"])
        elif cmd == "skip" and mint:
            self.db.skip(mint, until=next_report(report_time=self.cfg["desk"]["report_time"]))
            if p:
                self.pending.pop(mint)
                self.db.x("UPDATE signals SET status='skipped' WHERE id=?", (p["sid"],))

    def expire(self, now=None):
        now = now or time.time()
        for mint, p in list(self.pending.items()):
            if now >= p["deadline"]:
                self.pending.pop(mint)
                self.db.x("UPDATE signals SET status='missed' WHERE id=?", (p["sid"],))
                self.dlog.info("%s status=missed mode=LIVE", mint)

    async def run(self):
        self.beat("ok", f"{'PAPER' if self.paper else 'LIVE'} · waiting for Analyst")
        self.night_check()

        async def signals():
            while True:
                sig = await self.inbox_get(self.inbox, f"{'PAPER' if self.paper else 'LIVE'} · waiting for Analyst")
                await self.wait_unpaused()  # STEP 9 per-agent pause (controls + expiry keep running)
                await self.handle(sig)

        async def controls():
            while True:
                self.on_control(await self.inbox_get(self.control))

        async def ticker():
            last_night = night_start(report_time=self.cfg["desk"]["report_time"])
            while True:
                await asyncio.sleep(10)
                self.expire()
                ns = night_start(report_time=self.cfg["desk"]["report_time"])
                if ns != last_night:
                    last_night = ns
                    self.night_check()

        await asyncio.gather(signals(), controls(), ticker())
