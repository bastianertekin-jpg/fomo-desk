"""Offline fakes: no RPC, RugCheck, DexScreener, Jev or Telegram calls in tests."""
from __future__ import annotations

import json
from pathlib import Path

from agents.common import Ctx, SourceError, parse_pair
from bus import Bus
from config import test_config
from db import DB

RAW = Path(__file__).resolve().parents[1] / "logs" / "raw"
MINT = "31SYoqWFhx3i9UsfnoXTjFp4ZwJA8F8UPrMxymfDpump"
POOL = "HmcVp73ePenjn2JDHQBws6sYN56aBCmPuGDGouqzNUok"
CREATOR = "HN9YV4zbrsSjGwrLkr17ebbmCv99U34j7FuRcDtHjtZ9"


def pair(**kw) -> dict:
    p = parse_pair(json.loads((RAW / "dexscreener_pair_one.json").read_text()))
    p.update(kw)
    return p


def mint_js(mint_auth=None, freeze_auth=None, supply=1_000_000_000_000_000, decimals=6):
    return {"result": {"value": {"data": {"parsed": {"info": {
        "mintAuthority": mint_auth, "freezeAuthority": freeze_auth, "supply": str(supply), "decimals": decimals}}}}}}


def token_acct(amount, delegate=None):
    return {"pubkey": "x", "account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": amount}, "delegate": delegate}}}}}


class FakeSources:
    def __init__(self, **kw):
        self.pairs = kw.get("pairs", {})
        self.mint = kw.get("mint", mint_js())
        self.dev_balance = kw.get("dev_balance", 0.0)
        self.largest_ = kw.get("largest", [("poolacct", 100_000_000.0)] + [(f"h{i}", 10_000_000.0) for i in range(12)])
        self.owners_ = kw.get("owners", {"poolacct": POOL})
        self.report_ = kw.get("report", json.loads((RAW / "rugcheck_report.json").read_text()))
        self.graph_ = kw.get("graph", [])
        self.gpa = kw.get("gpa", [token_acct(5.0) for _ in range(20)])
        self.fail = set(kw.get("fail", ()))
        self.calls: list[str] = []

    def _f(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise SourceError(f"fake {name} down")

    async def dex_batch(self, mints):
        self._f("dex")
        return {m: self.pairs[m] for m in mints if m in self.pairs}

    async def market(self, mint):
        return self.pairs.get(mint)

    async def mint_account(self, mint):
        self._f("mint")
        return self.mint

    async def supply(self, mint):
        self._f("supply")
        return 1_000_000_000.0

    async def owner_balance(self, owner, mint):
        self._f("dev")
        return self.dev_balance

    async def largest(self, mint):
        self._f("largest")
        return self.largest_

    async def owners(self, addrs):
        self._f("owners")
        return {a: self.owners_.get(a, "someone") for a in addrs}

    async def program_accounts(self, mint):
        self._f("gpa")
        return self.gpa

    async def report(self, mint):
        self._f("report")
        return self.report_

    async def graph(self, mint):
        self._f("graph")
        return self.graph_


def make_ctx(tmp_path, sources=None, **cfg_overrides):
    db = DB(tmp_path / "t.db")
    cfg = test_config(**cfg_overrides)
    return Ctx(cfg=cfg, db=db, bus=Bus(), http=None, offline=True, sources=sources or FakeSources())
