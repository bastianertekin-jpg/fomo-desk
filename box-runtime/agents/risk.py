"""AGENT 5 · RISK — decides exits and sends the Sell button. Never places an order. Rules don't bend.

Input: db.positions (entry, size, peak, stop line, trail flag, opened_at) + settings; bus "fill" announces new ones.
POLL every risk.poll_s: ONE DexScreener batch for all open mints -> db.prices.
RULES every poll, in order: peak = max(peak, price); stop: price <= stop line; trail on at +trail_start_pct (never off);
trail: price <= peak * (1 - trail_pct); timeout: open >= timeout_h and price < entry * (1 + timeout_min_gain_pct).
Exit price = the polled price that fired it. PAPER closes at once (PnL minus fees.py both sides).
LIVE stays "exiting" (exit_reason set, closed_at NULL) until [Sold]; no tap in confirm_window_min -> push once more.
Never moves a stop down: a new stop setting applies to open positions only if it is tighter. Can't be paused.
"""
from __future__ import annotations

import asyncio
import time

import fees
import links
import notifier
from agents.common import Agent, RateLimited, SourceError, iso, store_price

NO_PRICE_POLLS = 4


def stop_line(pos: dict, cfg_r: dict) -> float:
    """Current stop line: the position's own stop, or the settings stop if that is tighter (higher). Never lower."""
    entry = pos["entry_price"]
    fresh = entry * (1 - cfg_r["stop_pct"] / 100)
    own = pos.get("stop_price")
    return fresh if own is None else max(own, fresh)


def stop_pct_of(pos: dict) -> float:
    return round((1 - pos["stop_price"] / pos["entry_price"]) * 100, 2)


def trail_line(pos: dict, cfg_r: dict) -> float | None:
    return pos["peak_price"] * (1 - cfg_r["trail_pct"] / 100) if pos.get("trail_active") else None


def evaluate(pos: dict, price: float, now: float, cfg_r: dict) -> tuple[dict, str | None]:
    """Pure. -> (updated {peak_price, stop_price, trail_active}, exit reason or None). Rules in the documented order."""
    entry = pos["entry_price"]
    peak = max(pos.get("peak_price") or entry, price)
    stop = stop_line(pos, cfg_r)
    trail = bool(pos.get("trail_active"))
    upd = {"peak_price": peak, "stop_price": stop, "trail_active": int(trail)}
    if price <= stop:
        return upd, "stop"
    if not trail and price >= entry * (1 + cfg_r["trail_start_pct"] / 100):
        trail = True
        upd["trail_active"] = 1
    if trail and price <= peak * (1 - cfg_r["trail_pct"] / 100):
        return upd, "trail"
    if now - pos["opened_at"] >= cfg_r["timeout_h"] * 3600 and price < entry * (1 + cfg_r["timeout_min_gain_pct"] / 100):
        return upd, "timeout"
    return upd, None


def pnl_of(pos: dict, exit_price: float, disc: float) -> dict:
    """PnL minus fees.py on both sides (buy on the ticket, sell on the exit value)."""
    size = pos["size_usd"] or 0
    gross = size * exit_price / pos["entry_price"] if pos["entry_price"] else 0
    buy_fee, sell_fee = fees.side_fee_usd(size, disc), fees.side_fee_usd(gross, disc)
    fee = buy_fee + sell_fee
    pnl = gross - size - fee
    return {"gross": gross, "buy_fee": buy_fee, "sell_fee": sell_fee, "fees": round(fee, 4), "pnl": round(pnl, 4),
            "pct": round(pnl / size * 100, 2) if size else 0.0}


class Risk(Agent):
    name = "risk"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.fills = self.bus.subscribe("fill")
        self.control = self.bus.subscribe("control")
        self.no_price: dict[int, int] = {}
        self.exiting: dict[int, dict] = {}  # LIVE: position id -> {repushed, at}
        self.last_exit = None

    # ---- push
    def push(self, mint, text, kind):
        paper = self.cfg["desk"].get("mode", "PAPER") != "LIVE"
        self.bus.publish("notify", {"text": ("PAPER · " if paper else "") + text, "mint": mint, "kind": kind,
                                    "buttons": notifier.sell_buttons(mint)})

    def exit_text(self, reason, name, pos, r):
        pnl, pct = r["pnl"], r["pct"]
        cr = self.my
        if reason == "trail":
            return f"Sold {name} · {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({pct:+.1f}%)"
        if reason == "stop":
            return f"{name} hit the -{stop_pct_of(pos):g}% stop · -${abs(pnl):,.2f}"
        if reason == "timeout":
            return f"Sell {name}, dead money · {pct:+.1f}% in {cr['timeout_h']:g}h"
        if reason == "pulled":
            return f"Sell {name} now, liquidity pulled · {pct:+.1f}%"
        return f"Sold {name} · {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({pct:+.1f}%)"

    # ---- exits
    def exit(self, pos: dict, reason: str, price: float) -> dict:
        mint = pos["mint"]
        name = self.coin_name(mint)
        mode = (pos.get("mode") or "PAPER").upper()
        r = pnl_of(pos, price, self.cfg["desk"]["fees_ref_discount"])
        now = time.time()
        paper = mode == "PAPER"
        if paper:
            self.db.x("UPDATE positions SET closed_at=?, exit_price=?, exit_reason=?, pnl_usd=?, fees_usd=? WHERE id=?",
                      (now, price, reason, r["pnl"], r["fees"], pos["id"]))
            # buy fee already left cash at the fill: the exit credits exit value minus the sell-side fee
            self.db.set_cash_usd(self.db.cash_usd() + r["gross"] - r["sell_fee"])
            self.db.add_balance(self.db.equity_usd())
            self.record_activity(pos, reason, name, r)
        else:  # LIVE: exiting until [Sold]
            self.db.x("UPDATE positions SET exit_price=?, exit_reason=? WHERE id=?", (price, reason, pos["id"]))
            self.exiting[pos["id"]] = {"at": now, "repushed": False}
        self.push(mint, self.exit_text(reason, name, pos, r), f"exit_{reason}")
        self.last_exit = "Stopped" if reason == "stop" else "Trailed out" if reason == "trail" else reason
        out = {"agent": "risk", "mint": mint, "name": name, "ts": iso(now), "reason": reason,
               "entryPrice": pos["entry_price"], "exitPrice": price, "peakPrice": pos.get("peak_price"),
               "pnlUsd": r["pnl"], "pct": r["pct"], "feesUsd": r["fees"], "stopPct": stop_pct_of(pos),
               "mode": mode, "status": "closed" if paper else "exiting", "link": links.coin_url(mint, "solana")}
        self.dlog.info("EXIT %s", out)
        return out

    def record_activity(self, pos, reason, name, r):
        sub = {"stop": f"Risk · -{stop_pct_of(pos):g}% stop", "trail": "Risk · trailing stop",
               "timeout": "Risk · dead money", "pulled": "Risk · liquidity pulled",
               "manual": "Risk · closed by owner"}.get(reason, f"Risk · {reason}")
        if reason == "stop":
            self.activity("stopped", pos["mint"], f"Stopped {name}", sub, r["pnl"])
        else:
            self.activity("sold", pos["mint"], f"Sold {name}", sub, r["pnl"])

    def confirm_sold(self, mint):
        """LIVE [Sold] tap: close the exiting position at its exit price."""
        for p in self.db.open_positions():
            if p["mint"] == mint and p["exit_reason"]:
                r = pnl_of(p, p["exit_price"], self.cfg["desk"]["fees_ref_discount"])
                self.db.x("UPDATE positions SET closed_at=?, pnl_usd=?, fees_usd=? WHERE id=?",
                          (time.time(), r["pnl"], r["fees"], p["id"]))
                self.record_activity(p, p["exit_reason"], self.coin_name(mint), r)
                self.exiting.pop(p["id"], None)

    # ---- poll
    def step(self, pos: dict, pair: dict | None, now: float) -> dict | None:
        """One position after one poll. Returns exit JSON or None."""
        cr = self.my
        if pos.get("exit_reason"):  # LIVE exiting: re-push once after confirm_window_min
            e = self.exiting.get(pos["id"])
            if e and not e["repushed"] and now - e["at"] >= cr["confirm_window_min"] * 60:
                e["repushed"] = True
                self.push(pos["mint"], f"{self.coin_name(pos['mint'])} still open", "still_open")
            return None
        price = (pair or {}).get("price_usd")
        if pair is not None and (pair.get("liquidity_usd") is not None and pair["liquidity_usd"] <= 0):
            last = self.db.last_price(pos["mint"])
            return self.exit(pos, "pulled", (last or {}).get("price") or pos["entry_price"])
        if not price:
            n = self.no_price[pos["id"]] = self.no_price.get(pos["id"], 0) + 1
            self.dlog.info("%s price=None polls_without_price=%d", pos["mint"], n)
            if n == NO_PRICE_POLLS:
                self.push(pos["mint"], f"{self.coin_name(pos['mint'])}: no price for 1 min", "no_price")
            return None
        self.no_price.pop(pos["id"], None)
        store_price(self.db, pair, pos["mint"])
        upd, reason = evaluate(pos, price, now, cr)
        self.db.x("UPDATE positions SET peak_price=?, stop_price=?, trail_active=? WHERE id=?",
                  (upd["peak_price"], upd["stop_price"], upd["trail_active"], pos["id"]))
        pos.update(upd)
        self.dlog.info("%s price=%s peak=%s stop=%s trail=%s action=%s", pos["mint"], price, upd["peak_price"],
                       upd["stop_price"], trail_line(pos, cr), reason or "hold")
        return self.exit(pos, reason, price) if reason else None

    async def poll_once(self, now=None):
        opens = self.db.open_positions()
        if not opens:
            return []
        pairs = await self.ctx.src.dex_batch([p["mint"] for p in opens])
        now = now or time.time()
        return [o for p in opens if (o := self.step(p, pairs.get(p["mint"]), now))]

    async def run(self):
        self.beat("ok", "idle")

        async def poller():
            missed = 0
            while True:
                await self.wait_unpaused()  # STEP 9: only pausable with no open position (enforced by commands.py)
                try:
                    await self.poll_once()
                    if missed:
                        self.log.warning("dexscreener back after %d missed polls", missed)
                        missed = 0
                except RateLimited:
                    missed += 1
                    self.log.warning("dexscreener 429; backing off 30s (missed polls: %d)", missed)
                    await asyncio.sleep(30)
                    continue
                except SourceError as e:
                    self.beat("degraded", str(e))
                n = len(self.db.open_positions())
                c = self.my
                self.beat("ok", (f"Guarding {n}" if n else (self.last_exit or "idle")) +
                          f" · stop -{c['stop_pct']:g}% · trail {c['trail_pct']:g}%")
                await asyncio.sleep(self.my["poll_s"])

        async def fills():
            while True:
                f = await self.fills.get()
                self.log.info("guarding %s from fill", f.get("mint"))

        async def controls():
            while True:
                c = await self.control.get()
                if c.get("cmd") == "sold" and c.get("mint"):
                    self.confirm_sold(c["mint"])
                elif c.get("cmd") == "close":  # owner /close: PAPER close at last polled price
                    for p in self.db.open_positions():
                        if c.get("mint") in (None, p["mint"]) and not p["exit_reason"]:
                            self.exit(p, "manual", self.db.mark(p))

        await asyncio.gather(poller(), fills(), controls())
