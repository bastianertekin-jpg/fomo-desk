"""fees.py — FOMO fee model on Solana (STEP 11). Used for PAPER PnL and the LIVE [Bought]/[Sold] books.

Per side (buy on the ticket, sell on the exit value), then minus desk.fees_ref_discount (0.10 = 10% off via the ref):
  $5 to $47.50   -> 2% of the ticket        (at $47.50 exactly: 0.95, same as the flat bracket, no jump)
  $47.50 to $190 -> flat $0.95              (at $190 exactly: 0.95 = 0.50% of 190, no jump)
  above $190     -> 0.50% of the ticket
Under $5 is not a FOMO ticket (the desk min_ticket is 47.50): charged as the 2% bracket so PAPER never undercounts.
Gas = 0: FOMO pays it. The desk never places an order; these numbers are bookkeeping only.
"""
from __future__ import annotations

LOW_TOP = 47.50
FLAT_TOP = 190.0
LOW_PCT = 0.02
FLAT_USD = 0.95
HIGH_PCT = 0.005
GAS_USD = 0.0


def raw_side_fee_usd(ticket_usd: float) -> float:
    """FOMO fee on one side before the referral discount."""
    t = max(0.0, float(ticket_usd))
    if t < LOW_TOP:
        return t * LOW_PCT
    if t <= FLAT_TOP:
        return FLAT_USD
    return t * HIGH_PCT


def bracket(ticket_usd: float) -> str:
    t = float(ticket_usd)
    return "2%" if t < LOW_TOP else "flat 0.95" if t <= FLAT_TOP else "0.50%"


def side_fee_usd(ticket_usd: float, fees_ref_discount: float = 0.10) -> float:
    """Fee on one side (buy or sell), then minus the referral discount. Gas is 0."""
    return raw_side_fee_usd(ticket_usd) * (1.0 - float(fees_ref_discount)) + GAS_USD


def gas_usd(_ticket_usd: float = 0.0) -> float:
    """Solana gas on FOMO: FOMO pays it."""
    return GAS_USD


def round_trip_fees_usd(ticket_usd: float, fees_ref_discount: float = 0.10, exit_usd: float | None = None) -> float:
    """Buy side on the ticket + sell side on the exit value (same as the ticket if exit_usd is None)."""
    out = ticket_usd if exit_usd is None else exit_usd
    return side_fee_usd(ticket_usd, fees_ref_discount) + side_fee_usd(out, fees_ref_discount)
