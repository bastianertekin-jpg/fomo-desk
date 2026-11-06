"""Shared agent plumbing: context, base class, market/RPC sources (all raw keys via fields.py).

Agents talk only through bus.py and db.py. Every network read goes through `Sources` so tests can
swap in fakes (no live Jev, Telegram, RPC or DexScreener calls in tests).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

import fields as F
from config import decision_log

DEX_TOKEN_URL = "https://api.dexscreener.com/tokens/v1/solana/{mint}"
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_GRAPH_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/insiders/graph"
PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
DEX_BATCH = 30  # DexScreener tokens/v1 takes up to 30 addresses per call


def iso(ts: float | None = None) -> str:
    return dt.datetime.fromtimestamp(time.time() if ts is None else ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short(addr: str | None) -> str:
    return f"{addr[:4]}...{addr[-3:]}" if addr and len(addr) > 8 else (addr or "?")


def money_k(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v / 1000:.1f}K" if abs(v) >= 1000 else f"{v:.0f}"


def num(v, nd=2) -> str:
    """Compact number for UI strings: 16.8, 0.9, 43."""
    if v is None:
        return "?"
    r = round(float(v), nd)
    return f"{r:g}"


def agent_paused(ctx, name: str) -> bool:
    return name in (ctx.state.get("paused_agents") or ())


@dataclass
class Ctx:
    cfg: dict
    db: object
    bus: object
    http: httpx.AsyncClient | None = None
    offline: bool = False
    state: dict = field(default_factory=lambda: {"paused": False, "paused_agents": set(), "started": time.time()})
    sources: object | None = None  # agents.common.Sources (or a fake in tests)

    @property
    def src(self) -> "Sources":
        if self.sources is None:
            self.sources = Sources(self)
        return self.sources


class Agent:
    name = "agent"

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.db = ctx.db
        self.bus = ctx.bus
        self.log = logging.getLogger(f"desk.{self.name}")
        self.dlog = decision_log(self.name)
        self.detail = "starting"

    @property
    def my(self) -> dict:
        """This agent's STEP 10 settings section, read live (app edits apply on the next decision)."""
        return self.cfg.get(self.name, {})

    @property
    def paused(self) -> bool:
        """STEP 9 per-agent pause ("pause Scanner"). Desk-wide "pause" (no new entries) is ctx.state["paused"]."""
        return agent_paused(self.ctx, self.name)

    async def wait_unpaused(self, every: float = 1.0):
        """Hold this agent's input loop while it's paused (its bus queue keeps the messages)."""
        if self.paused:
            self.beat("paused", "paused by owner")
            while self.paused:
                await asyncio.sleep(every)
            self.beat("ok", "resumed")

    def beat(self, status="ok", detail=None):
        if detail is not None:
            self.detail = detail
        self.db.beat(self.name, status, self.detail)

    async def inbox_get(self, inbox, detail=None):
        """Wait for the next bus message, heartbeating while idle so health.py stays green."""
        every = float(self.cfg.get("heartbeat_every_s") or 30)
        while True:
            try:
                return await asyncio.wait_for(inbox.get(), timeout=every)
            except asyncio.TimeoutError:
                self.beat("ok", detail if detail is not None else self.detail)

    def activity(self, kind, mint, title, subtitle=None, value=None):
        """STEP 4 feed row (bought|sold|stopped|rejected)."""
        self.db.activity(kind, mint=mint, agent=self.name, title=title, subtitle=subtitle, value=value)
        self.log.info("%s %s %s · %s", kind, mint, title, subtitle or "")

    def coin_name(self, mint, fallback=None) -> str:
        c = self.db.coin(mint) or {}
        return c.get("name") or c.get("symbol") or fallback or short(mint)

    async def run(self):  # pragma: no cover - overridden
        raise NotImplementedError

    async def guarded(self):
        """Run forever; restart on crash with backoff."""
        backoff = 2
        while True:
            try:
                await self.run()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.beat("error", f"{type(e).__name__}: {str(e)[:120]}")
                self.log.exception("agent crashed; restarting in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------- DexScreener
def parse_pair(p: dict) -> dict:
    """Normalise one DexScreener pair via fields.DEXSCREENER_PAIR. Missing -> None."""
    g = lambda k: F.get(p, F.DEXSCREENER_PAIR, k)  # noqa: E731
    created = g("pair_created_at")
    return {
        "mint": g("mint"), "symbol": g("symbol"), "name": g("name"),
        "pair_address": g("pair_address"), "dex_id": g("dex_id"), "chain_id": g("chain_id"),
        "price_usd": F.fnum(g("price_usd")), "price_native": F.fnum(g("price_native")),
        "liquidity_usd": F.fnum(g("liquidity_usd")), "market_cap": F.fnum(g("market_cap")),
        "fdv": F.fnum(g("fdv")),
        "volume_m5": F.fnum(g("volume_m5")), "volume_h1": F.fnum(g("volume_h1")),
        "buys_m5": g("txns_m5_buys"), "sells_m5": g("txns_m5_sells"),
        "buys_h1": g("txns_h1_buys"), "sells_h1": g("txns_h1_sells"),
        "change_m5": F.fnum(g("price_change_m5")), "change_h1": F.fnum(g("price_change_h1")),
        "change_h24": F.fnum(g("price_change_h24")),
        "pair_created_at": (created / 1000.0) if isinstance(created, (int, float)) else None,
    }


def pumpswap_pair(raw_pairs: list[dict]) -> dict | None:
    """The PumpSwap pair (dexId pumpswap); if several, the one with the most liquidity. None = unknown yet."""
    ps = [parse_pair(p) for p in raw_pairs or [] if F.get(p, F.DEXSCREENER_PAIR, "dex_id") == "pumpswap"]
    ps = [p for p in ps if p["mint"]]
    return max(ps, key=lambda p: p["liquidity_usd"] or 0) if ps else None


best_pair = pumpswap_pair  # back-compat name


class RateLimited(Exception):
    pass


class SourceError(Exception):
    pass


class RpcRateLimited(SourceError):
    """RPC answered 429 (or the method is still on its 429 cooldown)."""


def store_price(db, pair: dict | None, mint: str | None = None):
    if pair and pair.get("price_usd") is not None:
        db.add_price(mint or pair["mint"], pair["price_usd"], pair.get("volume_m5"))
        if pair.get("price_native"):
            try:
                db.set_setting("_sol_usd", pair["price_usd"] / pair["price_native"])
            except ZeroDivisionError:
                pass


# ---------------------------------------------------------------- base58 / PDA (pure python)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_P = 2 ** 255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P == 0:
        return True
    x = x * pow(2, (_P - 1) // 4, _P) % _P
    return (x * x - x2) % _P == 0


def find_program_address(seeds: list[bytes], program_id: str) -> str:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA")


def bonding_curve(mint: str) -> str:
    """pump.fun bonding curve PDA ["bonding-curve", mint] — excluded from top 10."""
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)


# ---------------------------------------------------------------- names
def norm_name(s: str | None) -> str:
    """Copycat key: lowercase, strip spaces, digits, emoji (keep letters only)."""
    return "".join(ch for ch in (s or "").lower() if ch.isalpha())


# ---------------------------------------------------------------- Sources
class Sources:
    """Every outside read the agents make. One instance per process; tests pass a fake with the same methods."""

    # RPC pacing: public mainnet-beta 429s hard on indexed calls (getTokenLargestAccounts). Space every call,
    # and after a 429 put that method on a cooldown (exponential backoff) instead of hammering it every poll.
    RPC_MIN_INTERVAL_S = 0.25
    RPC_COOLDOWN_START_S = 30.0
    RPC_COOLDOWN_MAX_S = 900.0
    RPC_SHORT_COOLDOWN_S = 4.0  # first cooldown for served methods (getProgramAccounts, getTokenSupply, ...)
    RPC_WAIT_MAX_S = 20.0       # served methods wait out a cooldown up to this long instead of failing
    TOP10_TTL_S = 300.0        # a computed top10 is reused for 5 min per mint
    TOP10_FAIL_TTL_S = 45.0    # a failed top10 is not retried for 45s per mint
    REPORT_TTL_S = 60.0        # rugcheck report / gPA reused across scanner+auditor for a minute

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self._id = 0
        self._rpc_lock = asyncio.Lock()
        self._rpc_last = 0.0
        self._cooldown: dict[str, float] = {}      # method -> monotonic time it may be called again
        self._backoff: dict[str, float] = {}       # method -> current cooldown length
        self._ttl: dict[tuple, tuple[float, object]] = {}
        self.top10_cache: dict[str, tuple[float, float | None, str]] = {}  # mint -> (ts, value|None, source/err)

    def _cache_get(self, key, ttl):
        hit = self._ttl.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return True, hit[1]
        return False, None

    def _cache_put(self, key, val):
        if len(self._ttl) > 2000:
            cutoff = time.monotonic() - 600
            self._ttl = {k: v for k, v in self._ttl.items() if v[0] >= cutoff}
        self._ttl[key] = (time.monotonic(), val)

    def rpc_cooling(self, method: str) -> float:
        """Seconds left on this method's 429 cooldown (0 = callable)."""
        return max(0.0, self._cooldown.get(method, 0.0) - time.monotonic())

    @property
    def http(self):
        if self.ctx.offline or self.ctx.http is None:
            raise SourceError("offline")
        return self.ctx.http

    # DexScreener
    async def dex_batch(self, mints) -> dict[str, dict]:
        """Batched tokens/v1 reads -> {mint: pumpswap pair}. Raises RateLimited on 429. Mints unknown = absent."""
        mints = [m for m in dict.fromkeys(mints) if m]
        out: dict[str, dict] = {}
        for i in range(0, len(mints), DEX_BATCH):
            chunk = mints[i:i + DEX_BATCH]
            try:
                r = await asyncio.wait_for(self.http.get(DEX_TOKEN_URL.format(mint=",".join(chunk)), timeout=10), 25)
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                raise SourceError(f"dexscreener {type(e).__name__}") from e
            if r.status_code == 429:
                raise RateLimited("dexscreener 429")
            if r.status_code != 200:
                raise SourceError(f"dexscreener HTTP {r.status_code}")
            try:
                data = r.json()
            except ValueError as e:
                raise SourceError("dexscreener bad json") from e
            by: dict[str, list] = {}
            for raw in data if isinstance(data, list) else []:
                by.setdefault(F.get(raw, F.DEXSCREENER_PAIR, "mint"), []).append(raw)
            for m in chunk:
                p = pumpswap_pair(by.get(m, []))
                if p:
                    out[m] = p
        return out

    async def market(self, mint) -> dict | None:
        try:
            return (await self.dex_batch([mint])).get(mint)
        except (SourceError, RateLimited):
            return None

    # Solana RPC
    async def rpc(self, method: str, params: list, _retry: bool = True):
        left = self.rpc_cooling(method)
        if left > 0:
            # cheap/served methods: just wait out a short cooldown; the indexed largest-accounts call fails fast
            if method != "getTokenLargestAccounts" and left <= self.RPC_WAIT_MAX_S:
                await asyncio.sleep(left)
            else:
                raise RpcRateLimited(f"rpc {method} 429 cooldown {left:.0f}s")
        async with self._rpc_lock:  # pace calls: at most one every RPC_MIN_INTERVAL_S
            wait = self._rpc_last + self.RPC_MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._rpc_last = time.monotonic()
        self._id += 1
        url = self.ctx.cfg["desk"]["rpc_url"]
        try:
            r = await asyncio.wait_for(
                self.http.post(url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                               timeout=httpx.Timeout(30, connect=10)), 45)
            js = r.json()
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as e:
            raise SourceError(f"rpc {method} {type(e).__name__}") from e
        err = js.get("error") if isinstance(js, dict) else None
        if r.status_code == 429 or (isinstance(err, dict) and err.get("code") in (429, -32429, -32005)):
            start = self.RPC_COOLDOWN_START_S if method == "getTokenLargestAccounts" else self.RPC_SHORT_COOLDOWN_S
            back = min(self._backoff.get(method, start / 2) * 2, self.RPC_COOLDOWN_MAX_S)
            self._backoff[method] = back
            self._cooldown[method] = time.monotonic() + back
            if _retry and method != "getTokenLargestAccounts" and back <= self.RPC_WAIT_MAX_S:
                return await self.rpc(method, params, _retry=False)  # waits out the cooldown, one retry
            raise RpcRateLimited(f"rpc {method} HTTP 429 (cooldown {back:.0f}s)")
        if r.status_code != 200 or err is not None:
            raise SourceError(f"rpc {method} HTTP {r.status_code} {str(err)[:80]}")
        self._backoff.pop(method, None)
        return js

    async def mint_account(self, mint) -> dict:
        js = await self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if F.dig(js, ("result", "value")) is None:
            raise SourceError("mint account missing")
        return js

    async def supply(self, mint) -> float | None:
        js = await self.rpc("getTokenSupply", [mint])
        return F.fnum(F.get(js, F.RPC_TOKEN_SUPPLY, "ui_amount"))

    async def owner_balance(self, owner, mint) -> float:
        js = await self.rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
        accts = F.dig(js, ("result", "value")) or []
        return sum(F.fnum(F.get(a, F.RPC_TOKEN_ACCOUNT, "token_amount")) or 0.0 for a in accts)

    async def largest(self, mint) -> list[tuple[str, float]]:
        js = await self.rpc("getTokenLargestAccounts", [mint])
        vals = F.dig(js, ("result", "value")) or []
        return [(v.get("address"), F.fnum(v.get("uiAmount")) or 0.0) for v in vals if isinstance(v, dict)]

    async def owners(self, addresses) -> dict[str, str | None]:
        if not addresses:
            return {}
        js = await self.rpc("getMultipleAccounts", [list(addresses), {"encoding": "jsonParsed"}])
        vals = F.dig(js, ("result", "value")) or []
        return {a: F.get(v or {}, F.RPC_PARSED_ACCOUNT, "owner") for a, v in zip(addresses, vals)}

    async def program_accounts(self, mint) -> list[dict]:
        """Every token account of this mint on Token AND Token-2022 (jsonParsed). Cached REPORT_TTL_S per mint."""
        hit, val = self._cache_get(("gpa", mint), self.REPORT_TTL_S)
        if hit:
            return val
        out = []
        for prog, filters in ((TOKEN_PROGRAM, [{"dataSize": 165}, {"memcmp": {"offset": 0, "bytes": mint}}]),
                              (TOKEN_2022_PROGRAM, [{"memcmp": {"offset": 0, "bytes": mint}}])):
            js = await self.rpc("getProgramAccounts", [prog, {"encoding": "jsonParsed", "filters": filters}])
            res = js.get("result")
            if not isinstance(res, list):
                raise SourceError("getProgramAccounts bad result")
            out.extend(res)
        self._cache_put(("gpa", mint), out)
        return out

    # RugCheck
    async def _get_json(self, url):
        try:
            r = await asyncio.wait_for(self.http.get(url, timeout=15), 30)
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            raise SourceError(f"rugcheck {type(e).__name__}") from e
        if r.status_code != 200:
            raise SourceError(f"rugcheck HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise SourceError("rugcheck bad json") from e

    async def report(self, mint) -> dict:
        hit, val = self._cache_get(("report", mint), self.REPORT_TTL_S)
        if hit:
            return val
        val = await self._get_json(RUGCHECK_URL.format(mint=mint))
        self._cache_put(("report", mint), val)
        return val

    async def graph(self, mint):
        return await self._get_json(RUGCHECK_GRAPH_URL.format(mint=mint))


def _top10_from(holders, skip: set, supply: float) -> float | None:
    """holders: [(token account, owner, ui amount)]; pool + bonding curve excluded by account or owner."""
    if not supply:
        return None
    kept = [amt for a, o, amt in holders if a not in skip and o not in skip and amt]
    return round(sum(sorted(kept, reverse=True)[:10]) / supply * 100, 2)


async def _top10_largest(src, mint, skip, supply):
    """On-chain: getTokenLargestAccounts + owners. Public RPC usually 429s this; Sources puts it on cooldown."""
    supply = supply if supply else await src.supply(mint)
    if not supply:
        return None
    largest = await src.largest(mint)
    owners = await src.owners([a for a, _ in largest])
    return _top10_from([(a, owners.get(a), amt) for a, amt in largest], skip, supply)


async def _top10_gpa(src, mint, skip, supply):
    """On-chain fallback: every token account via getProgramAccounts (served by public RPC, unlike the
    indexed getTokenLargestAccounts). Same numbers, just a heavier call."""
    accts = await src.program_accounts(mint)
    if not accts:
        return None
    supply = supply if supply else await src.supply(mint)
    rows = []
    for a in accts:
        amt = F.fnum(F.get(a, F.RPC_TOKEN_ACCOUNT, "token_amount"))
        rows.append((a.get("pubkey"), F.get(a, F.RPC_TOKEN_ACCOUNT, "owner"), amt or 0.0))
    return _top10_from(rows, skip, supply)


async def _top10_rugcheck(src, mint, skip, supply):
    """Last resort: RugCheck report topHolders (pct of supply per token account, with owner)."""
    rep = await src.report(mint)
    th = (rep or {}).get("topHolders") if isinstance(rep, dict) else None
    if not th:
        return None
    pcts = [F.fnum(h.get("pct")) or 0.0 for h in th
            if isinstance(h, dict) and h.get("address") not in skip and h.get("owner") not in skip]
    return round(sum(sorted(pcts, reverse=True)[:10]), 2)


async def top10_pct(src, mint: str, pool: str | None, supply: float | None = None) -> float | None:
    """Top 10 holders % of supply, pool and bonding curve excluded (by token-account owner or address).

    Tries getTokenLargestAccounts (skipped while on 429 cooldown), then getProgramAccounts, then RugCheck
    topHolders. Results are cached per mint on a real Sources (TOP10_TTL_S; failures TOP10_FAIL_TTL_S).
    Raises SourceError only when every source failed.
    """
    cache = src.top10_cache if isinstance(src, Sources) else None
    if cache is not None:
        hit = cache.get(mint)
        if hit:
            ts, val, why = hit
            age = time.monotonic() - ts
            if val is not None and age < Sources.TOP10_TTL_S:
                return val
            if val is None and age < Sources.TOP10_FAIL_TTL_S:
                raise SourceError(f"top10 recently failed ({why})")
    skip = {x for x in (pool, bonding_curve(mint)) if x}
    errors = []
    val, used = None, None
    steps = [("largest", _top10_largest)]
    if hasattr(src, "program_accounts"):
        steps.append(("gpa", _top10_gpa))
    if hasattr(src, "report"):
        steps.append(("rugcheck", _top10_rugcheck))
    for name, fn in steps:
        if name == "largest" and isinstance(src, Sources) and src.rpc_cooling("getTokenLargestAccounts") > 0:
            errors.append("largest: 429 cooldown")
            continue
        try:
            val = await fn(src, mint, skip, supply)
        except SourceError as e:
            errors.append(f"{name}: {str(e)[:60]}")
            continue
        if val is not None:
            used = name
            break
    if cache is not None:
        if len(cache) > 3000:
            cutoff = time.monotonic() - Sources.TOP10_TTL_S
            for k in [k for k, v in cache.items() if v[0] < cutoff]:
                cache.pop(k, None)
        cache[mint] = (time.monotonic(), val, used or "; ".join(errors)[:120])
    if val is None and errors:
        raise SourceError("top10 all sources failed: " + "; ".join(errors))
    if val is not None and used != "largest":
        logging.getLogger("desk.top10").debug("top10 %s via %s = %s", mint, used, val)
    return val


def state_hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def dumps(o) -> str:
    return json.dumps(o, separators=(",", ":"), default=str)
