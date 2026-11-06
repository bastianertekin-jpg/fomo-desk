"""Disabled pump.fun order ledger. Builds unsigned transactions and stops.

The signer refuses every hand-off, so a row never reaches awaiting confirmation
from this process. Fills happen only when a test or a future confirmer records
a signature and both token and quote deltas. The desk's own positions,
activity, balance, signals and scans are not written.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from agents.common import b58decode
from agents.risk import evaluate as risk_evaluate
from pump_curve import (
    Built,
    Curve,
    FailClosed,
    MintView,
    PoolView,
    build_amm,
    build_curve_buy,
    build_curve_sell,
    classify,
    decode_curve,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    intent_id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    status TEXT NOT NULL,
    venue TEXT,
    tokens INTEGER,
    limit_atoms INTEGER,
    fee_atoms INTEGER,
    notional_usd REAL,
    decimals INTEGER,
    message BLOB,
    reason TEXT,
    note TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    provenance TEXT NOT NULL DEFAULT 'unsigned',
    created_at REAL,
    updated_at REAL,
    signature_ok INTEGER,
    token_delta INTEGER,
    quote_delta INTEGER,
    sol_usd REAL
);
CREATE TABLE IF NOT EXISTS mint_locks (
    mint TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    acquired_at REAL
);
CREATE TABLE IF NOT EXISTS live_positions (
    mint TEXT PRIMARY KEY,
    buy_intent TEXT,
    qty INTEGER,
    entry_price REAL,
    size_usd REAL,
    peak_price REAL,
    stop_price REAL,
    trail_active INTEGER,
    opened_at REAL,
    status TEXT,
    exit_reason TEXT,
    closed_at REAL,
    pnl_usd REAL
);
"""

LIMITS = (
    "max_position_usd", "max_exposure_usd", "max_positions", "max_daily_loss_usd",
    "max_quote_age_s", "max_slippage_bps", "max_price_impact_bps",
    "fee_reserve_lamports", "fee_cap_lamports",
)
OPEN = ("open", "exit_failed")
INFLIGHT = ("built_unsigned", "awaiting_confirm")
UNFILLED = ("built_unsigned", "awaiting_confirm", "confirmed_unfilled")
BUILD_CAP = 3
NETWORKS = ("mainnet-beta", "devnet")


class SignerDisabled(FailClosed):
    def __init__(self):
        super().__init__("signer_disabled")


class DisabledSigner:
    """No key is loaded. Every call refuses."""

    def sign(self, message: bytes) -> None:
        raise SignerDisabled()


@dataclass
class Ticket:
    intent_id: str
    user: str
    mint: MintView
    curve: Curve | None
    pool: PoolView | None
    tokens: int
    protocol_bps: int | None
    creator_bps: int | None
    buyback: str
    blockhash: bytes | None
    observed_at: float | None
    sol_usd: float | None
    lamports: int | None
    now: float
    pumpswap: bool = False


def evaluate_arm(who: str, typed: str | None, limits: dict | None, *,
                 night_done: bool, reviewed: bool) -> tuple[bool, list[str]]:
    """Every condition, every time. desk.mode is not an input."""
    bad: list[str] = []
    if who == "telegram":
        bad.append("telegram")
    if typed != "LIVE":
        bad.append("typed_live")
    if not night_done:
        bad.append("paper_night")
    if not reviewed:
        bad.append("review")
    if not isinstance(limits, dict):
        return False, bad + ["limits"]
    if limits.get("kill_switch") is not False:
        bad.append("kill_switch")
    wallet = limits.get("wallet")
    try:
        raw = b58decode(wallet) if type(wallet) is str else b""
    except Exception:
        raw = b""
    if len(raw) != 32:
        bad.append("wallet")
    if limits.get("network") not in NETWORKS:
        bad.append("network")
    for key in LIMITS:
        value = limits.get(key)
        if type(value) is not int and type(value) is not float:
            bad.append(key)
        elif value < 0:
            bad.append(key)
    return (not bad), bad


def ingest(items: list[dict], *, enabled: bool) -> dict:
    """Classify supplied account bytes. Disabled returns nothing and does not trade."""
    if not enabled:
        return {"enabled": False, "rows": []}
    rows = []
    for item in items:
        mint = item.get("mint_view")
        try:
            curve = decode_curve(item["data"])
            phase = classify(curve, mint)
            rows.append({"mint": mint.mint, "phase": phase, "tradable": False,
                         "reason": "execution_disabled"})
        except FailClosed as exc:
            rows.append({"mint": getattr(mint, "mint", None), "phase": None,
                         "tradable": False, "reason": exc.reason})
        except (AttributeError, TypeError):
            rows.append({"mint": getattr(mint, "mint", None), "phase": None,
                         "tradable": False, "reason": "not_a_bonding_curve"})
    return {"enabled": True, "rows": rows}


class Ledger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def get(self, intent_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM orders WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row) if row else None

    def position(self, mint: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM live_positions WHERE mint=?", (mint,)).fetchone()
        return dict(row) if row else None

    def reconcile(self) -> dict:
        """Restart: a broadcast that was awaiting confirmation is held, not retried or filled."""
        held = 0
        rows = self.conn.execute(
            "SELECT intent_id, note FROM orders WHERE status='awaiting_confirm'").fetchall()
        for row in rows:
            if row["note"] != "reconcile_hold":
                self.conn.execute(
                    "UPDATE orders SET note='reconcile_hold', updated_at=? WHERE intent_id=?",
                    (time.time(), row["intent_id"]))
            held += 1
        return {"held": held, "retried": 0, "filled": 0}

    def prepare(self, ticket: Ticket, *, who: str, typed: str | None, limits: dict | None,
                night_done: bool, reviewed: bool) -> dict:
        existing = self.get(ticket.intent_id)
        if existing:
            return existing
        ok, reasons = evaluate_arm(who, typed, limits, night_done=night_done, reviewed=reviewed)
        if not ok:
            return self._store_rejected(ticket, ";".join(reasons))
        side = _side(ticket)
        if self._unfilled(ticket.mint.mint, side) >= BUILD_CAP:
            return self._store_rejected(ticket, "attempts_exhausted")
        if not self._lock(ticket.mint.mint, ticket.intent_id, ticket.now):
            return self._store_rejected(ticket, "mint_locked")
        try:
            self._age(ticket, limits)
            built = self._build(ticket, limits)
            self._afford(ticket, built, limits, side)
        except FailClosed as exc:
            self._unlock(ticket.mint.mint, ticket.intent_id)
            return self._store_rejected(ticket, exc.reason)
        return self._store_built(ticket, built, side)

    def hand_off(self, intent_id: str, signer: DisabledSigner | None = None) -> None:
        row = self.get(intent_id)
        if row is None or row["status"] != "built_unsigned":
            raise FailClosed("not_built")
        (signer or DisabledSigner()).sign(row["message"])

    def observe(self, intent_id: str, *, signature_ok: bool, token_delta: int | None,
                quote_delta: int | None, now: float) -> dict:
        """Record a result that something else already saw. Does not retry."""
        row = self.get(intent_id)
        if row is None or row["status"] != "awaiting_confirm":
            raise FailClosed("not_awaiting")
        good = signature_ok and token_delta is not None and quote_delta is not None and (
            (row["side"] == "buy" and token_delta > 0 and quote_delta < 0) or
            (row["side"] == "sell" and token_delta < 0 and quote_delta > 0))
        if not good:
            self.conn.execute(
                "UPDATE orders SET status='confirmed_unfilled', signature_ok=?, token_delta=?, "
                "quote_delta=?, provenance='chain_unfilled', updated_at=? WHERE intent_id=?",
                (int(bool(signature_ok)), token_delta, quote_delta, now, intent_id))
            self._unlock(row["mint"], intent_id)
            return self.get(intent_id)
        self.conn.execute(
            "UPDATE orders SET status='filled', signature_ok=1, token_delta=?, quote_delta=?, "
            "provenance='chain_delta', updated_at=? WHERE intent_id=?",
            (token_delta, quote_delta, now, intent_id))
        if row["side"] == "buy":
            self._open_position(row, token_delta, quote_delta, now)
        else:
            self._close_position(row, quote_delta, now)
            self.conn.execute("DELETE FROM mint_locks WHERE mint=?", (row["mint"],))
        return self.get(intent_id)

    def plan_exit(self, mint: str, price: float, now: float, risk_cfg: dict, ticket: Ticket, *,
                  who: str, typed: str | None, limits: dict | None, night_done: bool,
                  reviewed: bool) -> dict | None:
        pos = self.position(mint)
        if pos is None or pos["status"] not in OPEN:
            return None
        try:
            upd, reason = risk_evaluate(pos, price, now, risk_cfg)
        except (KeyError, TypeError) as exc:
            return self._exit_failed(pos, ticket, f"risk_cfg:{type(exc).__name__}", now)
        self.conn.execute(
            "UPDATE live_positions SET peak_price=?, stop_price=?, trail_active=? WHERE mint=?",
            (upd["peak_price"], upd["stop_price"], upd["trail_active"], mint))
        if not reason:
            return None
        ticket.intent_id = f"{pos['buy_intent']}:exit:{reason}"
        ticket.tokens = int(pos["qty"])
        existing = self.get(ticket.intent_id)
        if existing:
            return existing
        ok, reasons = evaluate_arm(who, typed, limits, night_done=night_done, reviewed=reviewed)
        if not ok:
            return self._exit_failed(pos, ticket, ";".join(reasons), now)
        try:
            self._age(ticket, limits)
            built = self._build(ticket, limits)
            if built.side != "sell":
                raise FailClosed("unsupported_phase")
            self._fee_only(built, limits)
        except FailClosed as exc:
            return self._exit_failed(pos, ticket, exc.reason, now)
        self.conn.execute(
            "UPDATE live_positions SET status='open', exit_reason=? WHERE mint=?", (reason, mint))
        return self._store_built(ticket, built, "sell", note=reason)

    # ---- internals
    def _build(self, ticket: Ticket, limits: dict) -> Built:
        slip, impact = limits.get("max_slippage_bps"), limits.get("max_price_impact_bps")
        curve = ticket.curve
        if curve is not None and not curve.complete:
            if _side(ticket) == "sell":
                return build_curve_sell(
                    ticket.user, ticket.mint, curve, ticket.tokens, ticket.protocol_bps,
                    ticket.creator_bps, slip, impact, ticket.buyback, ticket.blockhash)
            return build_curve_buy(
                ticket.user, ticket.mint, curve, ticket.tokens, ticket.protocol_bps,
                ticket.creator_bps, slip, impact, ticket.buyback, ticket.blockhash)
        if ticket.pool is not None:
            return build_amm(
                ticket.user, ticket.mint, ticket.pool, ticket.tokens, _side(ticket),
                slip, impact, ticket.blockhash, bool(ticket.pumpswap))
        raise FailClosed("unsupported_phase")

    def _age(self, ticket: Ticket, limits: dict) -> None:
        cap = limits.get("max_quote_age_s")
        if type(cap) not in (int, float) or ticket.observed_at is None:
            raise FailClosed("quote_age")
        if ticket.now - ticket.observed_at > cap or ticket.observed_at - ticket.now > 5:
            raise FailClosed("quote_age")

    def _afford(self, ticket: Ticket, built: Built, limits: dict, side: str) -> None:
        self._fee_only(built, limits)
        if ticket.sol_usd is None or ticket.sol_usd <= 0:
            raise FailClosed("usd_price_unset")
        if ticket.lamports is None:
            raise FailClosed("fee_reserve_balance_unset")
        notional = built.quote.limit_atoms / 1_000_000_000 * ticket.sol_usd
        built.quote.notional = notional  # type: ignore[attr-defined]
        if side == "sell":
            if ticket.lamports < limits["fee_reserve_lamports"]:
                raise FailClosed("fee_reserve")
            return
        if notional > limits["max_position_usd"]:
            raise FailClosed("max_position_usd")
        exposure = self._exposure() + notional
        if exposure > limits["max_exposure_usd"]:
            raise FailClosed("max_exposure_usd")
        if self._open_count() + 1 > limits["max_positions"]:
            raise FailClosed("max_positions")
        loss = self._day_loss(ticket.now - 86400)
        if loss >= limits["max_daily_loss_usd"]:
            raise FailClosed("max_daily_loss_usd")
        if ticket.lamports - built.quote.limit_atoms < limits["fee_reserve_lamports"]:
            raise FailClosed("fee_reserve")

    def _fee_only(self, built: Built, limits: dict) -> None:
        cap = limits.get("fee_cap_lamports")
        if type(cap) not in (int, float) or built.quote.fee_atoms > cap:
            raise FailClosed("fee_cap")

    def _store_built(self, ticket: Ticket, built: Built, side: str, note: str | None = None) -> dict:
        notional = getattr(built.quote, "notional", None)
        if notional is None and ticket.sol_usd:
            notional = built.quote.limit_atoms / 1_000_000_000 * ticket.sol_usd
        self.conn.execute(
            "INSERT INTO orders(intent_id,mint,side,status,venue,tokens,limit_atoms,fee_atoms,"
            "notional_usd,decimals,message,reason,note,attempts,provenance,created_at,updated_at,sol_usd) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ticket.intent_id, ticket.mint.mint, side, "built_unsigned", built.venue,
             built.quote.tokens, built.quote.limit_atoms, built.quote.fee_atoms, notional,
             ticket.mint.decimals, built.message, None, note, 1, "unsigned", ticket.now,
             ticket.now, ticket.sol_usd))
        return self.get(ticket.intent_id)

    def _store_rejected(self, ticket: Ticket, reason: str) -> dict:
        self.conn.execute(
            "INSERT INTO orders(intent_id,mint,side,status,reason,attempts,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ticket.intent_id, ticket.mint.mint, _side(ticket), "rejected", reason, 0,
             "unsigned", ticket.now, ticket.now))
        return self.get(ticket.intent_id)

    def _exit_failed(self, pos: dict, ticket: Ticket, reason: str, now: float) -> dict:
        self.conn.execute(
            "UPDATE live_positions SET status='exit_failed', exit_reason=? WHERE mint=?",
            (reason, pos["mint"]))
        self.conn.execute(
            "INSERT INTO orders(intent_id,mint,side,status,reason,note,attempts,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ticket.intent_id, pos["mint"], "sell", "exit_failed", reason, reason, 0,
             "unsigned", now, now))
        return self.get(ticket.intent_id)

    def _open_position(self, row: dict, token_delta: int, quote_delta: int, now: float) -> None:
        # Size follows the observed quote spent, not the pre-trade cap.
        sol = row["sol_usd"] or 0
        size = abs(quote_delta) / 1_000_000_000 * sol
        decimals = int(row["decimals"] if row["decimals"] is not None else 0)
        entry = size * (10 ** decimals) / token_delta if token_delta else 0
        self.conn.execute(
            "INSERT INTO live_positions(mint,buy_intent,qty,entry_price,size_usd,peak_price,"
            "stop_price,trail_active,opened_at,status) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (row["mint"], row["intent_id"], token_delta, entry, size, entry, None, 0, now, "open"))

    def _close_position(self, row: dict, quote_delta: int, now: float) -> None:
        pos = self.position(row["mint"])
        if pos is None:
            return
        proceeds = (quote_delta / 1_000_000_000) * (row["sol_usd"] or 0)
        pnl = proceeds - (pos["size_usd"] or 0)
        self.conn.execute(
            "UPDATE live_positions SET status='closed', closed_at=?, pnl_usd=? WHERE mint=?",
            (now, pnl, row["mint"]))
        self.conn.execute("UPDATE orders SET notional_usd=? WHERE intent_id=? AND notional_usd IS NULL",
                          (proceeds, row["intent_id"]))

    def _lock(self, mint: str, intent_id: str, now: float) -> bool:
        try:
            self.conn.execute("INSERT INTO mint_locks(mint,intent_id,acquired_at) VALUES(?,?,?)",
                              (mint, intent_id, now))
            return True
        except sqlite3.IntegrityError:
            row = self.conn.execute("SELECT intent_id FROM mint_locks WHERE mint=?", (mint,)).fetchone()
            return bool(row and row["intent_id"] == intent_id)

    def _unlock(self, mint: str, intent_id: str) -> None:
        self.conn.execute("DELETE FROM mint_locks WHERE mint=? AND intent_id=?", (mint, intent_id))

    def _unfilled(self, mint: str, side: str) -> int:
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM orders WHERE mint=? AND side=? AND status IN ({','.join('?' * len(UNFILLED))})",
            (mint, side, *UNFILLED)).fetchone()
        return int(row["n"])

    def _exposure(self) -> float:
        inflight = self.conn.execute(
            "SELECT COALESCE(SUM(notional_usd),0) AS n FROM orders WHERE side='buy' AND status IN ('built_unsigned','awaiting_confirm')"
        ).fetchone()["n"]
        opened = self.conn.execute(
            "SELECT COALESCE(SUM(size_usd),0) AS n FROM live_positions WHERE status IN ('open','exit_failed')"
        ).fetchone()["n"]
        return float(inflight or 0) + float(opened or 0)

    def _open_count(self) -> int:
        inflight = self.conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE side='buy' AND status IN ('built_unsigned','awaiting_confirm')"
        ).fetchone()["n"]
        opened = self.conn.execute(
            "SELECT COUNT(*) AS n FROM live_positions WHERE status IN ('open','exit_failed')"
        ).fetchone()["n"]
        return int(inflight or 0) + int(opened or 0)

    def _day_loss(self, since: float) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(-pnl_usd),0) AS n FROM live_positions WHERE status='closed' AND pnl_usd<0 AND closed_at>=?",
            (since,)).fetchone()
        return float(row["n"] or 0)


def _side(ticket: Ticket) -> str:
    return "sell" if ":exit:" in ticket.intent_id else "buy"
