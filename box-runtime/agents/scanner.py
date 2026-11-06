"""AGENT 1 · SCANNER — counts and cuts. Owns every coin that graduates off the pump.fun curve onto PumpSwap.

Input: ONE PumpPortal websocket, {"method":"subscribeMigration"} only (no trade streams).
Every scanner.poll_s: one batched DexScreener read for the whole waiting room -> db.prices.
Kill on the first hit, in order: liquidity < min, h1 > max, copycat, top 10 > max (pool + curve excluded;
only fetched once the pool is near min_pool_age).
Pass when pool age >= min_pool_age_min and no kill hit -> bus "candidate" (to Auditor). Every decision -> db.scans.
After max_wait_min without passing: kill "never qualified". Never judges quality; never sends a mint twice a night.
"""
from __future__ import annotations

import asyncio
import json
import time

import websockets

import fields as F
from agents.common import (PUMPPORTAL_WS, Agent, RateLimited, SourceError, iso, money_k, norm_name, num, store_price,
                           top10_pct)
from db import night_start


def liq_kill(liq, cfg_s) -> str | None:
    if liq is None or liq < cfg_s["min_liquidity_usd"]:
        return f"liq ${money_k(liq)}"
    return None


def top10_kill(top10, cfg_s) -> str | None:
    if top10 is not None and top10 > cfg_s["max_top10_pct"]:
        return f"top 10 hold {top10:.0f}%"
    return None


def h1_kill(h1, cfg_s) -> str | None:
    if h1 is not None and h1 > cfg_s["max_h1_change_pct"]:
        return f"+{h1:.0f}% in 1h"
    return None


def copycat_of(name: str | None, symbol: str | None, runners: dict[str, str], mint: str | None = None) -> str | None:
    """runners: {mint: display name} of today's runners. Exact match or one inside the other (normalised).
    Keys shorter than 3 letters are ignored (a 1-letter name would match everything)."""
    mine = {k for k in (norm_name(name), norm_name(symbol)) if len(k) >= 3}
    for rmint, (rname, rsym) in runners.items():
        if rmint == mint:
            continue
        for r in (norm_name(rname), norm_name(rsym)):
            if len(r) < 3:
                continue
            if any(m == r or m in r or r in m for m in mine):
                return f"copycat of {rname or rsym}"
    return None


class Scanner(Agent):
    name = "scanner"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.room: dict[str, dict] = {}          # mint -> {migrated_at, signature}
        self.runners: dict[str, tuple] = {}      # mint -> (name, symbol), coins seen today with h24 >= runner_h24_pct
        self.runners_day = time.strftime("%Y-%m-%d")
        self.pools = 0
        self.reconnects: list[float] = []
        self._restore_room()

    # ---- waiting room
    def _restore_room(self):
        cutoff = time.time() - self.my["max_wait_min"] * 60
        for c in self.db.q("SELECT mint, migrated_at FROM coins WHERE migrated_at>=? AND mint NOT IN (SELECT mint FROM scans)",
                           (cutoff,)):
            self.room[c["mint"]] = {"migrated_at": c["migrated_at"]}

    def sent_tonight(self, mint) -> bool:
        ns = night_start(report_time=self.cfg["desk"]["report_time"])
        return self.db.one("SELECT 1 AS e FROM scans WHERE mint=? AND ts>=?", (mint, ns)) is not None

    async def on_migration(self, msg: dict):
        mint = F.get(msg, F.PUMPPORTAL_MIGRATION, "mint")
        if not mint or F.get(msg, F.PUMPPORTAL_MIGRATION, "tx_type") not in (None, "migrate"):
            return None
        if mint in self.room or self.sent_tonight(mint) or self.db.is_skipped(mint):
            return None
        now = time.time()
        self.room[mint] = {"migrated_at": now, "signature": F.get(msg, F.PUMPPORTAL_MIGRATION, "signature")}
        self.pools += 1
        creator = F.get(msg, F.PUMPPORTAL_MIGRATION, "creator")
        self.db.upsert_coin(mint, migrated_at=now, creator=creator)
        self.beat("ok", f"{self.pools} pools")
        await self.store_creator(mint, creator)
        return mint

    async def store_creator(self, mint, creator):
        """Creator + creator balance NOW (Auditor layer 3 'sold 0'). Unknown creator stays null."""
        src = self.ctx.src
        try:
            if not creator:
                creator = F.get(await src.report(mint), F.RUGCHECK_REPORT, "creator")
            if creator:
                bal = await src.owner_balance(creator, mint)
                self.db.upsert_coin(mint, creator=creator, creator_balance_at_migration=bal)
        except SourceError as e:
            self.log.info("creator read failed %s: %s", mint, e)

    # ---- decisions
    def decide(self, mint, verdict, reason, pair, top10, age):
        p = pair or {}
        out = {"agent": "scanner", "mint": mint, "name": p.get("name"), "symbol": p.get("symbol"),
               "pool": p.get("pair_address"), "liquidityUsd": p.get("liquidity_usd"), "top10Pct": top10,
               "h1ChangePct": p.get("change_h1"), "ageMin": int(age) if age is not None else None,
               "verdict": verdict, "reason": reason, "ts": iso()}
        self.db.add_scan(mint, verdict, reason, liquidity_usd=out["liquidityUsd"], top10_pct=top10,
                         h1_change_pct=out["h1ChangePct"], age_min=out["ageMin"])
        self.room.pop(mint, None)
        self.dlog.info("%s %s %s %s liq=%s top10=%s h1=%s age=%s", out["ts"], mint, verdict, reason,
                       out["liquidityUsd"], top10, out["h1ChangePct"], out["ageMin"])
        if verdict == "pass":
            self.bus.publish("candidate", out)
        return out

    async def evaluate(self, mint, pair, now=None):
        """One waiting-room coin after a DexScreener read. Returns the decision JSON or None (still waiting)."""
        now = now or time.time()
        info = self.room.get(mint) or {}
        age = (now - info["migrated_at"]) / 60 if info.get("migrated_at") else None
        if pair is None:  # DexScreener doesn't know it yet: wait in the room
            if age is not None and age >= self.my["max_wait_min"]:
                return self.decide(mint, "kill", "never qualified", None, None, age)
            return None
        self.db.upsert_coin(mint, name=pair.get("name"), symbol=pair.get("symbol"), pool=pair.get("pair_address"))
        s = self.my
        if r := liq_kill(pair.get("liquidity_usd"), s):
            return self.decide(mint, "kill", r, pair, None, age)
        # cheap kills first (no RPC): h1 and copycat come straight from the DexScreener pair
        if r := h1_kill(pair.get("change_h1"), s):
            return self.decide(mint, "kill", r, pair, None, age)
        if r := copycat_of(pair.get("name"), pair.get("symbol"), self.runners, mint):
            return self.decide(mint, "kill", r, pair, None, age)
        # top 10 (RPC / RugCheck) only once the pool is about old enough to pass; cached per mint in Sources
        near_age = age is None or age >= max(0.0, s["min_pool_age_min"] - 0.5)
        top10 = None
        if near_age:
            try:
                top10 = await top10_pct(self.ctx.src, mint, pair.get("pair_address"))
            except SourceError as e:
                self.log.info("top10 read failed %s: %s", mint, e)
                top10 = None
            if r := top10_kill(top10, s):
                return self.decide(mint, "kill", r, pair, top10, age)
        if top10 is not None and age is not None and age >= s["min_pool_age_min"]:
            return self.decide(mint, "pass", None, pair, top10, age)
        if age is not None and age >= s["max_wait_min"]:
            return self.decide(mint, "kill", "never qualified", pair, top10, age)
        return None

    def note_runners(self, pairs: dict):
        day = time.strftime("%Y-%m-%d")
        if day != self.runners_day:
            self.runners, self.runners_day = {}, day
        for m, p in pairs.items():
            if (p.get("change_h24") or 0) >= self.my["runner_h24_pct"]:
                self.runners[m] = (p.get("name"), p.get("symbol"))

    async def poll_once(self):
        mints = list(self.room)
        if not mints:
            return
        # stay under 250 DexScreener requests/min (30 mints per request)
        cap = max(1, int(250 * self.my["poll_s"] / 60)) * 30
        pairs = await self.ctx.src.dex_batch(mints[:cap])
        for m, p in pairs.items():
            store_price(self.db, p, m)
        self.note_runners(pairs)
        for m in mints[:cap]:
            await self.evaluate(m, pairs.get(m))

    async def poll_loop(self):
        while True:
            await self.wait_unpaused()  # STEP 9 "pause Scanner": migrations still queue in the room, no evaluations
            try:
                await self.poll_once()
                self.beat("ok", f"{self.pools} pools · {len(self.room)} waiting")
            except RateLimited:
                self.beat("degraded", "dexscreener 429 · waiting 30s")
                self.log.warning("dexscreener 429; keeping waiting room, sleeping 30s")
                await asyncio.sleep(30)
                continue
            except SourceError as e:
                self.beat("degraded", str(e))
            await asyncio.sleep(self.my["poll_s"])

    async def ws_loop(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(PUMPPORTAL_WS, ping_interval=20, open_timeout=15) as ws:
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    self.beat("ok", "pumpportal connected")
                    backoff = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)  # silent 120s -> reconnect
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(msg, dict):
                            asyncio.ensure_future(self.on_migration(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                now = time.time()
                self.reconnects = [t for t in self.reconnects if now - t < 3600] + [now]
                self.db.set_setting("_ws_reconnects", self.reconnects)
                self.log.warning("pumpportal %s; reconnect #%d in %ss", type(e).__name__, len(self.reconnects), backoff)
                self.beat("degraded", f"pumpportal reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def run(self):
        if self.ctx.offline:
            self.beat("idle", "offline mode")
            while True:
                await asyncio.sleep(3600)
        await asyncio.gather(self.ws_loop(), self.poll_loop())
