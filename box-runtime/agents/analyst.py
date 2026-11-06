"""AGENT 3 · ANALYST — never decides itself. Builds a STATE, asks Jev 4 closed "noul" questions, code applies the GATE.

Input: bus "audited" (Auditor JSON + Scanner JSON) + a fresh DexScreener read of the pool.
mode "jev": POST TypeSafe System One. mode "rules": same 4 names/0-1 range from DexScreener numbers.
A Jev error in jev mode never falls back to rules: reject "no answer".
pass (gate) -> bus "signal" (to Executor). reject -> Activity "Rejected X / Analyst · {first failed rule}".
"""
from __future__ import annotations

import asyncio
import time

import httpx

import fields as F
from agents.common import TYPESAFE_URL, Agent, iso, short, state_hash
from config import read_secret
from db import night_start

QUESTIONS = {
    "alive_6h": {"type": "noul", "instructions": "This coin is still actively traded 6 hours from now"},
    "organic": {"type": "noul", "instructions": "Most buyers in the last 30 minutes are separate real wallets"},
    "flush": {"type": "noul", "instructions": "Price just flushed down and buyers are absorbing it"},
    "chase": {"type": "noul", "instructions": "This entry is chasing a green candle that already ran"},
}
NAMES = tuple(QUESTIONS)
MAX_STATE = 3000


def _n(v, nd=0):
    if v is None:
        return "n/a"
    v = float(v)
    return f"{v:,.{nd}f}" if nd else f"{v:,.0f}"


def _p(v):
    return "n/a" if v is None else f"{float(v):+g}"


def build_state(name, age_min, pair: dict, facts: dict, max_chars: int = MAX_STATE) -> str:
    """Plain text, numbers only, no opinions, no thresholds/balance/'buy'."""
    p, f = pair or {}, facts or {}
    dev, top10 = f.get("dev_pct"), f.get("top10")
    s = (f"Coin {name} on Solana, graduated {int(age_min) if age_min is not None else 'n/a'} min ago. "
         f"Liquidity ${_n(p.get('liquidity_usd'))}. Market cap ${_n(p.get('market_cap'))}. "
         f"Last 5 min: {_n(p.get('buys_m5'))} buys, {_n(p.get('sells_m5'))} sells, volume ${_n(p.get('volume_m5'))}, "
         f"price {_p(p.get('change_m5'))}%. "
         f"Last 1h: {_n(p.get('buys_h1'))} buys, {_n(p.get('sells_h1'))} sells, volume ${_n(p.get('volume_h1'))}, "
         f"price {_p(p.get('change_h1'))}%. "
         f"Top 10 hold {'n/a' if top10 is None else f'{round(top10, 2):g}'}%. "
         f"Dev holds {'n/a' if dev is None else f'{round(dev, 2):g}'}%, sold 0. "
         f"LP {f.get('lp') or 'n/a'}. Bundled {f.get('bundled') if f.get('bundled') is not None else 'n/a'}. "
         f"Delegations {f.get('delegations') or 'n/a'}.")
    return s[:max_chars]


def gate(answers: dict, cfg_a: dict) -> tuple[bool, list[str], int]:
    """Code only, Jev never sees it. -> (passed, failed rules "<rule> <value>", score)."""
    a, o, fl, c = (float(answers[k]) for k in NAMES)
    score = round(100 * (a + o + fl + (1 - c)) / 4)
    failed = []
    if not a >= cfg_a["min_alive_6h"]:
        failed.append(f"alive_6h {a:g}")
    if not o >= cfg_a["min_organic"]:
        failed.append(f"organic {o:g}")
    if not c <= cfg_a["max_chase"]:
        failed.append(f"chase {c:g}")
    if not score >= cfg_a["min_score"]:
        failed.append(f"score {score}")
    return (not failed), failed, score


def clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def rules_answers(pair: dict, bundled: int | None, rules: dict) -> dict:
    """RULES MODE: computed in code from the DexScreener read."""
    p = pair or {}
    alive = clamp((p.get("volume_h1") or 0) / rules["alive_volume_h1"])
    organic = clamp((p.get("buys_h1") or 0) / rules["organic_buys_h1"])
    if bundled is not None and bundled >= rules.get("bundled_penalty_from", 3):
        organic *= rules["bundled_penalty"]
    m5 = p.get("change_m5")
    b5, s5 = p.get("buys_m5") or 0, p.get("sells_m5") or 0
    if m5 is not None and m5 <= -rules["flush_drop_m5_pct"] and b5 >= s5:
        flush = 1.0
    elif m5 is not None and m5 < 0:
        flush = 0.5
    else:
        flush = 0.0
    chase = clamp((p.get("change_h1") or 0) / rules["chase_h1_pct"])
    return {"alive_6h": round(alive, 4), "organic": round(organic, 4), "flush": flush, "chase": round(chase, 4)}


def parse_jev(js) -> dict | None:
    """answers.{name}.noul for the 4 names, each in [0,1]; else None (bad answer)."""
    ans = F.get(js, F.JEV_SYSTEMONE, "answers") if isinstance(js, dict) else None
    if not isinstance(ans, dict):
        return None
    out = {}
    for q in NAMES:
        v = F.fnum(F.dig(ans, (q, F.JEV_SYSTEMONE["noul"])))
        if v is None or not 0 <= v <= 1:
            return None
        out[q] = v
    return out


class NoAnswer(Exception):
    pass


class BadAnswer(Exception):
    pass


class Analyst(Agent):
    name = "analyst"
    retry_delay = 2.0
    queue_429_s = 60.0

    def __init__(self, ctx):
        super().__init__(ctx)
        self.inbox = self.bus.subscribe("audited")
        self.asked: set[str] = set()
        self.n = 0

    def asked_tonight(self, mint) -> bool:
        if mint in self.asked:
            return True
        ns = night_start(report_time=self.cfg["desk"]["report_time"])
        return self.db.one("SELECT 1 AS e FROM verdicts WHERE mint=? AND ts>=?", (mint, ns)) is not None

    async def ask_jev(self, state: str) -> tuple[dict, str, int]:
        """-> (answers, model, input_tokens). Raises NoAnswer / BadAnswer. Exactly the documented body, nothing else."""
        key = read_secret("TYPESAFE_API_KEY")
        if not key or self.ctx.state.get("no_paid_api") or self.ctx.offline or self.ctx.http is None:
            raise NoAnswer("no key / offline / paid API disabled")
        body = {"model": "jev-latest", "state": state, "questions": QUESTIONS}
        t_end = time.monotonic() + self.queue_429_s
        tries = 0
        while True:
            try:
                r = await self.ctx.http.post(TYPESAFE_URL, json=body, timeout=self.my["timeout_s"],
                                             headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError as e:
                r, err = None, type(e).__name__
            if r is not None and r.status_code == 429:
                if time.monotonic() >= t_end:
                    raise NoAnswer("429 queue expired")
                await asyncio.sleep(self.retry_delay)
                continue
            if r is not None and r.status_code == 200:
                try:
                    js = r.json()
                except ValueError:
                    self.dlog.info("bad answer raw=%r", r.text[:2000])
                    raise BadAnswer("not json")
                ans = parse_jev(js)
                if ans is None:
                    self.dlog.info("bad answer raw=%r", r.text[:2000])
                    raise BadAnswer("missing/out of range")
                tok = F.get(js, F.JEV_SYSTEMONE, "input_tokens")
                return ans, F.get(js, F.JEV_SYSTEMONE, "model") or "jev", int(tok or 0)
            tries += 1
            if tries >= 2:
                raise NoAnswer(f"HTTP {r.status_code if r is not None else err}")
            await asyncio.sleep(self.retry_delay)

    def finish(self, mint, name, answers, score, passed, failed, model, tokens, state, rules_mode, msg, pair):
        out = {"agent": "analyst", "mint": mint, "answers": answers, "score": score, "passed": passed,
               "failed": failed, "model": model, "inputTokens": tokens}
        reason = "pass" if passed else failed[0]
        self.db.add_verdict(mint, answers or {}, score, passed, f"{model} · {reason}", tokens)
        self.dlog.info("%s state=%s %s score=%s %s tokens=%s", mint, state_hash(state or ""),
                       " ".join(f"{k}={(answers or {}).get(k)}" for k in NAMES), score,
                       "PASS" if passed else f"REJECT {reason}", tokens)
        if passed:
            self.bus.publish("signal", {**out, "ctx": {
                "name": name, "scanner": msg.get("scanner") or {},
                "auditor": {k: v for k, v in msg.items() if k not in ("scanner", "facts")},
                "market": pair, "analystTs": iso(), "priceUsd": (pair or {}).get("price_usd")}})
        else:
            who = "Analyst (rules)" if rules_mode else "Analyst"
            self.activity("rejected", mint, f"Rejected {name}", f"{who} · {reason}")
            if self.cfg["desk"].get("push_rejects"):
                self.bus.publish("notify", {"text": f"Rejected {name} · {who} · {reason}", "mint": mint})
        return out

    async def analyse(self, msg: dict, pair: dict | None = None) -> dict | None:
        mint = msg["mint"]
        if self.asked_tonight(mint):
            self.log.info("ignore %s: already scored tonight", mint)
            return None
        self.asked.add(mint)
        scan = msg.get("scanner") or {}
        coin = self.db.coin(mint) or {}
        name = scan.get("name") or coin.get("name") or short(mint)
        mode = self.my.get("mode", "jev")
        rules_mode = mode == "rules"
        if pair is None:
            pair = await self.ctx.src.market(mint)
        self.n += 1
        self.beat("ok", f"scoring {name}")
        if not pair:
            return self.finish(mint, name, None, 0, False, ["no market data"], "rules" if rules_mode else "jev", 0,
                               "", rules_mode, msg, pair)
        mig = coin.get("migrated_at")
        age = (time.time() - mig) / 60 if mig else scan.get("ageMin")
        facts = msg.get("facts") or {}
        state = build_state(name, age, pair, facts)
        if rules_mode:
            answers = rules_answers(pair, facts.get("bundled"), self.my["rules"])
            model, tokens = "rules", 0
        else:
            try:
                answers, model, tokens = await self.ask_jev(state)
            except NoAnswer as e:
                self.log.info("jev no answer %s: %s", mint, e)
                return self.finish(mint, name, None, 0, False, ["no answer"], "jev", 0, state, False, msg, pair)
            except BadAnswer as e:
                self.log.info("jev bad answer %s: %s", mint, e)
                return self.finish(mint, name, None, 0, False, ["bad answer"], "jev", 0, state, False, msg, pair)
        passed, failed, score = gate(answers, self.my)
        out = self.finish(mint, name, answers, score, passed, failed, model, tokens, state, rules_mode, msg, pair)
        self.beat("ok", f"scored {self.n} · last {name} {score}")
        return out

    async def run(self):
        self.beat("ok", f"waiting ({self.my.get('mode')})")
        while True:
            msg = await self.inbox_get(self.inbox, f"waiting ({self.my.get('mode')})")
            await self.wait_unpaused()  # STEP 9 per-agent pause
            await self.analyse(msg)
