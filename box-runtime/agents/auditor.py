"""AGENT 2 · AUDITOR — takes every Scanner pass apart, 8 layers in order. One red layer and the coin is dead.

Input: Scanner JSON (bus "candidate") + db.coins row (creator, creator balance at migration, migratedAt).
Sources: RPC (desk.rpc_url) + RugCheck report + insiders graph, each fetched once per audit and reused.
Every source call: retry auditor.retries times, 2 s apart; still nothing = red "no data: {layer}". No data is never a pass.
Stops at the first red; the rest are written as "not checked". Each layer is emitted on bus "ui" (audit_layer) the
moment it finishes. passed -> bus "audited" (to Analyst). red -> Activity "Rejected X / Auditor · {reason}".
"""
from __future__ import annotations

import asyncio
import time

import fields as F
from agents.common import Agent, SourceError, iso, num, short, top10_pct
from db import night_start

RC = F.RUGCHECK_REPORT
G = F.RUGCHECK_INSIDERS_GRAPH
LAYERS = {1: "Mint authority", 2: "Freeze authority", 3: "Dev wallet", 4: "Top 10 holders", 5: "Liquidity",
          6: "Pool age", 7: "Bundled wallets", 8: "Delegations"}
NO_DATA = {1: "mint", 2: "freeze", 3: "dev", 4: "top 10", 5: "liquidity", 6: "pool age", 7: "bundles", 8: "delegations"}
LAYER8_DOWN = "layer 8 down, every coin is red until rpc_url changes"


class NoData(Exception):
    pass


# ---------------------------------------------------------------- pure helpers
def market_for_pool(report: dict | None, pool: str | None) -> dict | None:
    for m in F.get(report or {}, RC, "markets") or []:
        if isinstance(m, dict) and pool and F.get(m, RC, "m_pubkey") == pool:
            return m
    return None


def biggest_bundle(graph) -> int | None:
    """Biggest connected group of holders in the RugCheck insiders graph. None = no data (no edges key).
    graph = list of networks [{nodes, links}] (or one such dict). An empty list = no networks = 0."""
    nets = graph if isinstance(graph, list) else [graph] if isinstance(graph, dict) else None
    if nets is None:
        return None
    best = 0
    for net in nets:
        if not isinstance(net, dict) or G["edges"] not in net:
            return None
        holders = {n.get(G["node_id"]) for n in net.get(G["nodes"]) or []
                   if isinstance(n, dict) and (F.fnum(n.get(G["node_holdings"])) or 0) > 0}
        parent = {h: h for h in holders}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for e in net.get(G["edges"]) or []:
            a, b = e.get(G["edge_source"]), e.get(G["edge_target"])
            if a in parent and b in parent:
                parent[find(a)] = find(b)
        sizes: dict = {}
        for h in holders:
            r = find(h)
            sizes[r] = sizes.get(r, 0) + 1
        best = max([best, *sizes.values()])
    return best


def delegation_stats(accounts: list[dict]) -> tuple[int, str | None, int]:
    """(holders with balance > 0, biggest delegate, its holder-account count)."""
    holders = 0
    by: dict[str, int] = {}
    for a in accounts:
        amt = F.fnum(F.get(a, F.RPC_TOKEN_ACCOUNT, "token_amount"))
        if amt is None:
            amt = F.fnum(F.get(a, F.RPC_TOKEN_ACCOUNT, "raw_amount")) or 0
        if amt > 0:
            holders += 1
            d = F.get(a, F.RPC_GPA, "delegate")
            if d:
                by[d] = by.get(d, 0) + 1
    if not by:
        return holders, None, 0
    d = max(by, key=by.get)
    return holders, d, by[d]


def mint_supply(mint_js: dict) -> float | None:
    raw = F.fnum(F.get(mint_js, F.RPC_MINT_ACCOUNT, "supply"))
    dec = F.get(mint_js, F.RPC_MINT_ACCOUNT, "decimals")
    if raw is None or dec is None:
        return None
    return raw / (10 ** int(dec))


class Auditor(Agent):
    name = "auditor"
    retry_delay = 2.0

    def __init__(self, ctx, subscribe: bool = True, dry: bool = False):
        super().__init__(ctx)
        self.inbox = self.bus.subscribe("candidate") if subscribe else None
        self.dry = dry  # STEP 9 owner "buy X" check: no db rows, no bus, no alerts
        self.taken = self.passed = self.rejected = 0
        self.layer8_alerted_night = None

    # ---- sources with retry + per-audit cache
    async def _get(self, cache: dict, key: str, fn):
        if key in cache:
            if isinstance(cache[key], NoData):
                raise cache[key]
            return cache[key]
        last = None
        for i in range(int(self.my.get("retries", 3))):
            try:
                cache[key] = await fn()
                return cache[key]
            except SourceError as e:
                last = e
                if i < int(self.my.get("retries", 3)) - 1:
                    await asyncio.sleep(self.retry_delay)
        self.log.info("no data %s after retries: %s", key, last)
        cache[key] = NoData(str(last))
        raise cache[key]

    def already_rejected(self, mint) -> bool:
        ns = night_start(report_time=self.cfg["desk"]["report_time"])
        return self.db.one("SELECT 1 AS e FROM audits WHERE mint=? AND pass=0 AND ts>=?", (mint, ns)) is not None

    async def layer(self, n: int, mint: str, scan: dict, coin: dict, cache: dict, now: float) -> tuple[bool, str, str]:
        """-> (pass, ui value, source). Raises NoData."""
        s, src = self.my, self.ctx.src
        if n in (1, 2, 3):
            mjs = await self._get(cache, "mint", lambda: src.mint_account(mint))
        if n == 1:
            ok = F.get(mjs, F.RPC_MINT_ACCOUNT, "mint_authority") is None
            return ok, "Revoked" if ok else "mint not revoked", "rpc"
        if n == 2:
            ok = F.get(mjs, F.RPC_MINT_ACCOUNT, "freeze_authority") is None
            return ok, "Revoked" if ok else "freeze not revoked", "rpc"
        if n == 3:
            supply = mint_supply(mjs)
            if not supply:
                return False, "no supply", "rpc"
            creator, at_mig = coin.get("creator"), coin.get("creator_balance_at_migration")
            if not creator or at_mig is None:
                return False, "no data: dev", "rpc"
            cache["supply"] = supply
            bal = await self._get(cache, "dev", lambda: src.owner_balance(creator, mint))
            pct = bal / supply * 100
            cache["dev_pct"] = pct
            if pct >= s["max_dev_pct"]:
                return False, f"dev holds {pct:.0f}%", "rpc"
            if bal < at_mig:
                return False, "dev sold", "rpc"
            return True, f"{num(pct, 1)}% · sold 0", "rpc"
        if n == 4:
            t = await self._get(cache, "top10", lambda: top10_pct(src, mint, scan.get("pool"), cache.get("supply")))
            if t is None:
                raise NoData("top10")
            cache["top10"] = t
            return (t < s["max_top10_pct"]), (f"{num(t, 1)}%" if t < s["max_top10_pct"] else f"top 10 hold {t:.0f}%"), "rpc"
        if n == 5:
            rep = await self._get(cache, "report", lambda: src.report(mint))
            m = market_for_pool(rep, scan.get("pool"))
            pct = F.fnum(F.get(m, RC, "m_lp_locked_pct")) if m else None
            if pct is None:
                return False, "no data: liquidity", "rugcheck"
            if pct < s["min_lp_locked_pct"]:
                return False, "liquidity unlocked", "rugcheck"
            base, quote = F.fnum(F.get(m, RC, "m_lp_base_usd")), F.fnum(F.get(m, RC, "m_lp_quote_usd"))
            liq_now = (base or 0) + (quote or 0) if (base is not None or quote is not None) else None
            liq_scan = scan.get("liquidityUsd")
            if liq_now is not None and liq_scan and liq_now < liq_scan * 0.5:
                return False, "liquidity pulled", "rugcheck"
            cache["lp"] = f"{num(pct, 0)}% burned" if F.get(m, RC, "m_type") == "pump_fun_amm" else f"{num(pct, 0)}% locked"
            return True, cache["lp"], "rugcheck"
        if n == 6:
            mig = coin.get("migrated_at")
            if mig is None:
                return False, "no data: pool age", "db"
            age = (now - mig) / 60
            return (age >= s["min_pool_age_min"]), (f"{int(age)}m" if age >= s["min_pool_age_min"] else "pool too young"), "db"
        if n == 7:
            g = await self._get(cache, "graph", lambda: src.graph(mint))
            b = biggest_bundle(g)
            if b is None:
                return False, "no data: bundles", "rugcheck"
            cache["bundled"] = b
            if b >= s["max_bundled"]:
                return False, f"{b} bundled wallets", "rugcheck"
            return True, "None" if b < 2 else f"{b} wallets", "rugcheck"
        if n == 8:
            try:
                accts = await self._get(cache, "gpa", lambda: src.program_accounts(mint))
            except NoData:
                self.layer8_down()
                raise
            holders, d, cnt = delegation_stats(accts)
            cache["holders"] = holders
            if d and cnt > s["max_delegated_accounts"]:
                cache["delegations"] = f"{cnt} wallets → {short(d)}"
                return False, cache["delegations"], "rpc"
            cache["delegations"] = "None" if not d else f"{cnt} wallets → {short(d)}"
            return True, cache["delegations"], "rpc"
        raise ValueError(n)

    def layer8_down(self):
        if self.dry:
            return
        n = int(self.db.get_setting("_layer8_kills", 0) or 0) + 1
        self.db.set_setting("_layer8_kills", n)  # morning Page puts this count on top
        ns = night_start(report_time=self.cfg["desk"]["report_time"])
        if self.layer8_alerted_night != ns:
            self.layer8_alerted_night = ns
            self.bus.publish("notify", {"text": LAYER8_DOWN, "kind": "alert"})

    async def owner_check(self, mint: str, scan: dict | None = None, now: float | None = None) -> dict:
        """STEP 9 "buy X" for a coin never pushed tonight: run all 8 layers now, read-only.
        Writes nothing to db, publishes nothing (the coin never enters the desk, is never sized)."""
        now = now or time.time()
        coin = self.db.coin(mint) or {}
        scan = {"mint": mint, **(scan or {})}
        cache: dict = {}
        layers, reason = [], None
        for n in range(1, 9):
            try:
                ok, value, source = await self.layer(n, mint, scan, coin, cache, now)
            except NoData:
                ok, value, source = False, f"no data: {NO_DATA[n]}", "rpc" if n in (1, 2, 3, 4, 8) else "rugcheck"
            layers.append({"n": n, "name": LAYERS[n], "pass": ok, "value": value, "source": source})
            if not ok and reason is None:
                reason = f"layer {n} · {value}"
        return {"mint": mint, "passed": reason is None, "green": sum(1 for x in layers if x["pass"]),
                "layers": layers, "reason": reason}

    async def audit(self, scan: dict, now: float | None = None) -> dict | None:
        mint = scan["mint"]
        if self.already_rejected(mint):
            self.log.info("skip %s: rejected earlier tonight", mint)
            return None
        now = now or time.time()
        coin = self.db.coin(mint) or {}
        name = scan.get("name") or coin.get("name") or short(mint)
        self.taken += 1
        self.beat("ok", f"{self.taken} taken apart · taking apart {name}")
        if not self.my.get("enabled", True):
            out = {"agent": "auditor", "mint": mint, "ts": iso(), "passed": True, "layersChecked": 0, "layers": [],
                   "holders": None, "reason": "auditor disabled"}
            self.bus.publish("audited", {**out, "scanner": scan, "facts": {}})
            return out
        cache: dict = {}
        layers, reason, ts = [], None, time.time()
        raw_log = []
        for n in range(1, 9):
            t0 = time.monotonic()
            try:
                ok, value, source = await self.layer(n, mint, scan, coin, cache, now)
            except NoData:
                ok, value, source = False, f"no data: {NO_DATA[n]}", "rpc" if n in (1, 2, 3, 4, 8) else "rugcheck"
            ms = int((time.monotonic() - t0) * 1000)
            row = {"n": n, "name": LAYERS[n], "pass": ok, "value": value, "source": source}
            layers.append(row)
            raw_log.append(f"L{n}={value!r}/{ms}ms")
            self.db.add_audit(mint, str(n), LAYERS[n], value, ok, source, ts)
            self.bus.publish("ui", {"type": "audit_layer", "mint": mint, **row})
            if not ok:
                reason = value
                break
        for n in range(len(layers) + 1, 9):
            self.db.add_audit(mint, str(n), LAYERS[n], "not checked", None, None, ts)
        passed = reason is None
        if cache.get("holders") is not None:
            self.db.upsert_coin(mint, holders=cache["holders"])
        out = {"agent": "auditor", "mint": mint, "ts": iso(ts), "passed": passed, "layersChecked": len(layers),
               "layers": layers, "holders": cache.get("holders"), "reason": reason}
        self.dlog.info("%s %s %s", mint, " ".join(raw_log), "PASS 8/8" if passed else f"RED {reason}")
        if passed:
            self.passed += 1
            facts = {"dev_pct": cache.get("dev_pct"), "top10": cache.get("top10"), "lp": cache.get("lp"),
                     "bundled": cache.get("bundled"), "delegations": cache.get("delegations")}
            self.bus.publish("audited", {**out, "scanner": scan, "facts": facts})
        else:
            self.rejected += 1
            self.activity("rejected", mint, f"Rejected {name}", f"Auditor · {reason}")
            if self.cfg["desk"].get("push_rejects"):
                self.bus.publish("notify", {"text": f"Rejected {name} · Auditor · {reason}", "mint": mint})
        self.beat("ok", f"{self.taken} taken apart · {self.rejected} rejected")
        return out

    async def run(self):
        self.beat("ok", "waiting for Scanner")
        while True:
            scan = await self.inbox_get(self.inbox, "waiting for Scanner")
            await self.wait_unpaused()  # STEP 9 per-agent pause: hold the candidate until resumed
            await self.audit(scan)
