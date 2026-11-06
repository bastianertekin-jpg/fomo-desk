"""Official pump.fun instruction construction. Nothing here is sent.

Curve trades follow the public pump IDL and @pump-fun/pump-sdk 4.1.0:
buy_v3 / sell_v3 on 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P.
Migrated pools follow pump_amm buy_v2 / sell_v2 on
pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA, and only when the caller
explicitly enables that adapter for a migrated mint. Quotes stay inside the
curve or the supplied vaults. Cashback, mayhem, quote chains, non-SOL quotes,
and a buy that would finish the curve are refused. Fee rates and the buyback
recipient are arguments; this module does not invent them and does not call
the network.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from agents.common import (
    PUMP_PROGRAM,
    TOKEN_2022_PROGRAM,
    TOKEN_PROGRAM,
    b58decode,
    b58encode,
    find_program_address,
)

ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"

PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
FEE_CONFIG_SEED = b"fee_config"

# sha256("global:<name>")[:8] and sha256("account:<name>")[:8], checked
# against the public IDL.
BUY_V3 = bytes([7, 5, 29, 196, 245, 23, 101, 80])
SELL_V3 = bytes([28, 146, 222, 119, 38, 196, 105, 213])
BUY_V2 = bytes([184, 23, 238, 97, 103, 197, 211, 61])
SELL_V2 = bytes([93, 246, 130, 60, 231, 233, 64, 178])
BONDING_CURVE_DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])
POOL_DISC = bytes([241, 154, 109, 4, 17, 177, 109, 188])

BONDING_CURVE_SIZE = 166
COMPLETE_OFFSET = 48
ZERO = "11111111111111111111111111111111"

# Named gaps. None of these are filled in by guessing.
GAPS = (
    "pumpportal_new_token_feed_is_not_a_source",
    "buy_v3_refuses_cashback",
    "mayhem_curves_refused",
    "quote_chains_refused",
    "non_sol_quote_refused",
    "curve_completing_buy_refused",
    "pumpswap_off_unless_explicit",
    "fee_bps_and_buyback_recipient_required",
    "no_network_and_no_signer",
)


class FailClosed(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Curve:
    virtual_token: int
    virtual_quote: int
    real_token: int
    real_quote: int
    supply: int
    complete: bool
    creator: str
    mayhem: bool
    cashback: bool
    quote_mint: str
    creator_fee_bps: int
    holder_reward: bool
    depth: int
    raw_len: int = BONDING_CURVE_SIZE


@dataclass
class MintView:
    mint: str
    program: str
    decimals: int
    mint_authority: str | None
    freeze_authority: str | None


@dataclass
class Quote:
    tokens: int
    net_atoms: int
    fee_atoms: int
    limit_atoms: int
    impact_bps: int


@dataclass
class Built:
    venue: str
    side: str
    data: bytes
    accounts: list[dict]
    message: bytes
    quote: Quote


def _u64(n: int) -> bytes:
    if n < 0 or n > 2**64 - 1:
        raise FailClosed("u64_out_of_range")
    return struct.pack("<Q", n)


def _i128(n: int) -> bytes:
    if n < -(2**127) or n > 2**127 - 1:
        raise FailClosed("i128_out_of_range")
    return int(n).to_bytes(16, "little", signed=True)


def ceil_fee(amount: int, bps: int) -> int:
    if amount < 0 or bps < 0 or bps > 10_000:
        raise FailClosed("fee_bps")
    return (amount * bps + 9_999) // 10_000


def _pda(program: str, *seeds: bytes) -> str:
    return find_program_address(list(seeds), program)


def bonding_curve_pda(mint: str) -> str:
    return _pda(PUMP_PROGRAM, b"bonding-curve", b58decode(mint))


def event_authority(program: str) -> str:
    return _pda(program, b"__event_authority")


def global_pda() -> str:
    return _pda(PUMP_PROGRAM, b"global")


def amm_global_pda() -> str:
    return _pda(PUMP_AMM_PROGRAM, b"global_config")


def user_volume_pda(user: str) -> str:
    return _pda(PUMP_PROGRAM, b"user_volume_accumulator", b58decode(user))


def amm_user_volume_pda(user: str) -> str:
    return _pda(PUMP_AMM_PROGRAM, b"user_volume_accumulator", b58decode(user))


def fee_config_pda(program: str) -> str:
    return _pda(FEE_PROGRAM, FEE_CONFIG_SEED, b58decode(program))


def associated_token(owner: str, mint: str, token_program: str) -> str:
    return _pda(
        ASSOCIATED_TOKEN_PROGRAM,
        b58decode(owner),
        b58decode(token_program),
        b58decode(mint),
    )


def sol_quote(mint: str) -> bool:
    return mint in (ZERO, WSOL_MINT)


def normalize_quote(mint: str) -> str:
    if not sol_quote(mint):
        raise FailClosed("non_sol_quote")
    return WSOL_MINT


def encode_curve(c: Curve) -> bytes:
    raw = bytearray(BONDING_CURVE_SIZE)
    raw[0:8] = BONDING_CURVE_DISC
    struct.pack_into("<QQQQQ", raw, 8, c.virtual_token, c.virtual_quote,
                     c.real_token, c.real_quote, c.supply)
    raw[48] = 1 if c.complete else 0
    raw[49:81] = b58decode(c.creator)
    raw[81] = 1 if c.mayhem else 0
    raw[82] = 1 if c.cashback else 0
    raw[83:115] = b58decode(c.quote_mint)
    struct.pack_into("<Q", raw, 115, c.creator_fee_bps)
    raw[124] = 1 if c.holder_reward else 0
    raw[141] = c.depth
    return bytes(raw)


def decode_curve(raw: bytes) -> Curve:
    if len(raw) < COMPLETE_OFFSET + 1 or raw[:8] != BONDING_CURVE_DISC:
        raise FailClosed("not_a_bonding_curve")
    if len(raw) > BONDING_CURVE_SIZE and any(raw[BONDING_CURVE_SIZE:]):
        raise FailClosed("unknown_curve_tail")
    buf = raw.ljust(BONDING_CURVE_SIZE, b"\x00")
    vt, vq, rt, rq, supply = struct.unpack_from("<QQQQQ", buf, 8)
    return Curve(
        virtual_token=vt, virtual_quote=vq, real_token=rt, real_quote=rq,
        supply=supply, complete=bool(buf[48]),
        creator=b58encode(buf[49:81]), mayhem=bool(buf[81]),
        cashback=bool(buf[82]), quote_mint=b58encode(buf[83:115]),
        creator_fee_bps=struct.unpack_from("<Q", buf, 115)[0],
        holder_reward=bool(buf[124]), depth=buf[141], raw_len=len(raw),
    )


def classify(curve: Curve, mint: MintView) -> str:
    """Structural reject. A pass is still not tradable: execution stays off."""
    if mint.mint_authority or mint.freeze_authority:
        raise FailClosed("authority_set")
    if mint.program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise FailClosed("unknown_token_program")
    if mint.decimals < 0 or mint.decimals > 9:
        raise FailClosed("decimals")
    if curve.complete:
        raise FailClosed("curve_complete")
    if curve.mayhem:
        raise FailClosed("mayhem")
    if curve.cashback:
        raise FailClosed("cashback")
    if curve.depth != 0:
        raise FailClosed("quote_chain")
    if curve.virtual_token <= 0 or curve.virtual_quote <= 0 or curve.real_token <= 0:
        raise FailClosed("empty_reserves")
    normalize_quote(curve.quote_mint)
    return "bonding_curve"


def discovery_request() -> dict:
    """getProgramAccounts body. The running desk does not send it."""
    return {
        "method": "getProgramAccounts",
        "program": PUMP_PROGRAM,
        "sent": False,
        "source": "pump_program",
        "unsupported": ["pumpportal"],
        "filters": [
            {"memcmp": {"offset": 0, "bytes": b58encode(BONDING_CURVE_DISC)}},
            {"memcmp": {"offset": COMPLETE_OFFSET, "bytes": b58encode(bytes([0]))}},
        ],
    }


def _rates(curve: Curve, protocol_bps: int | None, creator_bps: int | None) -> tuple[int, int]:
    """Caller passes the already chosen schedule. A nonzero on-curve creator
    rate that disagrees with it is refused: the live gate that picks between
    them is not decoded here."""
    if protocol_bps is None or creator_bps is None:
        raise FailClosed("fee_bps_unset")
    if not 0 <= protocol_bps <= 10_000 or not 0 <= creator_bps <= 10_000:
        raise FailClosed("fee_bps")
    if curve.creator == ZERO:
        return protocol_bps, 0
    if curve.creator_fee_bps and curve.creator_fee_bps != creator_bps:
        raise FailClosed("creator_fee_conflict")
    return protocol_bps, creator_bps


def _slip(net: int, bps: int, up: bool) -> int:
    if bps is None or bps < 0:
        raise FailClosed("slippage_unset")
    if bps > 10_000:
        raise FailClosed("slippage")
    moved = net * bps // 10_000
    out = net + moved if up else net - moved
    if out <= 0:
        raise FailClosed("slippage")
    return out


def quote_buy(curve: Curve, tokens: int, protocol_bps: int | None,
              creator_bps: int | None, slippage_bps: int | None,
              impact_cap_bps: int | None) -> Quote:
    if tokens <= 0:
        raise FailClosed("amount")
    if tokens > curve.real_token:
        raise FailClosed("would_complete_curve")
    if slippage_bps is None:
        raise FailClosed("slippage_unset")
    if impact_cap_bps is None:
        raise FailClosed("price_impact_unset")
    protocol, creator = _rates(curve, protocol_bps, creator_bps)
    net = tokens * curve.virtual_quote // (curve.virtual_token - tokens) + 1
    fee = ceil_fee(net, protocol) + ceil_fee(net, creator)
    impact = net * 10_000 // curve.virtual_quote
    if impact > impact_cap_bps:
        raise FailClosed("price_impact")
    return Quote(tokens, net, fee, _slip(net + fee, slippage_bps, True), impact)


def quote_sell(curve: Curve, tokens: int, protocol_bps: int | None,
               creator_bps: int | None, slippage_bps: int | None,
               impact_cap_bps: int | None) -> Quote:
    if tokens <= 0:
        raise FailClosed("amount")
    if slippage_bps is None:
        raise FailClosed("slippage_unset")
    if impact_cap_bps is None:
        raise FailClosed("price_impact_unset")
    protocol, creator = _rates(curve, protocol_bps, creator_bps)
    gross = tokens * curve.virtual_quote // (curve.virtual_token + tokens)
    fee = ceil_fee(gross, protocol) + ceil_fee(gross, creator)
    net = gross - fee
    if net <= 0:
        raise FailClosed("fee_exceeds_output")
    impact = gross * 10_000 // curve.virtual_quote
    if impact > impact_cap_bps:
        raise FailClosed("price_impact")
    return Quote(tokens, net, fee, _slip(net, slippage_bps, False), impact)


def _meta(pubkey: str, writable: bool, signer: bool = False) -> dict:
    return {"pubkey": pubkey, "writable": writable, "signer": signer}


def curve_accounts(user: str, mint: str, quote_mint: str, base_program: str,
                   buyback: str) -> list[dict]:
    if not buyback or buyback == ZERO:
        raise FailClosed("buyback_recipient_unset")
    quote_mint = normalize_quote(quote_mint)
    curve = bonding_curve_pda(mint)
    return [
        _meta(global_pda(), False),
        _meta(mint, False),
        _meta(quote_mint, False),
        _meta(base_program, False),
        _meta(TOKEN_PROGRAM, False),
        _meta(curve, True),
        _meta(associated_token(curve, mint, base_program), True),
        _meta(associated_token(curve, quote_mint, TOKEN_PROGRAM), True),
        _meta(user, True, True),
        _meta(associated_token(user, mint, base_program), True),
        _meta(associated_token(user, quote_mint, TOKEN_PROGRAM), True),
        _meta(user_volume_pda(user), True),
        _meta(fee_config_pda(PUMP_PROGRAM), False),
        _meta(buyback, True),
        _meta(SYSTEM_PROGRAM, False),
        _meta(event_authority(PUMP_PROGRAM), False),
        _meta(PUMP_PROGRAM, False),
    ]


def build_curve_buy(user: str, mint: MintView, curve: Curve, tokens: int,
                    protocol_bps: int | None, creator_bps: int | None,
                    slippage_bps: int | None, impact_cap_bps: int | None,
                    buyback: str, blockhash: bytes) -> Built:
    classify(curve, mint)
    q = quote_buy(curve, tokens, protocol_bps, creator_bps, slippage_bps, impact_cap_bps)
    data = BUY_V3 + _u64(tokens) + _u64(q.limit_atoms) + bytes([0])
    accounts = curve_accounts(user, mint.mint, curve.quote_mint, mint.program, buyback)
    return Built("bonding_curve_v3", "buy", data, accounts,
                 compile_message(user, accounts, data, blockhash), q)


def build_curve_sell(user: str, mint: MintView, curve: Curve, tokens: int,
                     protocol_bps: int | None, creator_bps: int | None,
                     slippage_bps: int | None, impact_cap_bps: int | None,
                     buyback: str, blockhash: bytes) -> Built:
    classify(curve, mint)
    q = quote_sell(curve, tokens, protocol_bps, creator_bps, slippage_bps, impact_cap_bps)
    data = SELL_V3 + _u64(tokens) + _u64(q.limit_atoms)
    accounts = curve_accounts(user, mint.mint, curve.quote_mint, mint.program, buyback)
    return Built("bonding_curve_v3", "sell", data, accounts,
                 compile_message(user, accounts, data, blockhash), q)


@dataclass
class PoolView:
    pool: str
    base_vault: int
    quote_vault: int
    virtual_quote: int
    base_mint: str
    quote_mint: str
    pool_base: str
    pool_quote: str
    cashback: bool = False
    mayhem: bool = False
    disable_flags: int = 0
    protocol_bps: int | None = None
    creator_bps: int | None = None
    lp_bps: int | None = None
    buyback: str = ""


def _pool_ok(pool: PoolView, side: str) -> None:
    bit = 3 if side == "buy" else 4
    if pool.disable_flags & (1 << bit):
        raise FailClosed("pool_disabled")
    if pool.cashback or pool.mayhem:
        raise FailClosed("cashback" if pool.cashback else "mayhem")
    normalize_quote(pool.quote_mint)
    if pool.base_vault <= 0:
        raise FailClosed("empty_reserves")
    effective = pool.quote_vault + pool.virtual_quote
    if effective <= 0:
        raise FailClosed("empty_reserves")
    for name in ("protocol_bps", "creator_bps", "lp_bps"):
        rate = getattr(pool, name)
        if rate is None:
            raise FailClosed("fee_bps_unset")
        if not 0 <= rate <= 10_000:
            raise FailClosed("fee_bps")
    if not pool.buyback or pool.buyback == ZERO:
        raise FailClosed("buyback_recipient_unset")


def _amm_quote(pool: PoolView, tokens: int, side: str,
               slippage_bps: int | None, impact_cap_bps: int | None) -> Quote:
    _pool_ok(pool, side)
    if tokens <= 0 or slippage_bps is None or impact_cap_bps is None:
        raise FailClosed("amount" if tokens <= 0 else
                         "slippage_unset" if slippage_bps is None else "price_impact_unset")
    effective = pool.quote_vault + pool.virtual_quote
    if side == "buy":
        if tokens >= pool.base_vault:
            raise FailClosed("empty_reserves")
        net = (effective * tokens + (pool.base_vault - tokens) - 1) // (pool.base_vault - tokens)
        fee = sum(ceil_fee(net, r) for r in (pool.protocol_bps, pool.creator_bps, pool.lp_bps))
        impact = net * 10_000 // effective
        if impact > impact_cap_bps:
            raise FailClosed("price_impact")
        return Quote(tokens, net, fee, _slip(net + fee, slippage_bps, True), impact)
    gross = tokens * effective // (pool.base_vault + tokens)
    fee = sum(ceil_fee(gross, r) for r in (pool.protocol_bps, pool.creator_bps, pool.lp_bps))
    net = gross - fee
    if net <= 0:
        raise FailClosed("fee_exceeds_output")
    impact = gross * 10_000 // effective
    if impact > impact_cap_bps:
        raise FailClosed("price_impact")
    return Quote(tokens, net, fee, _slip(net, slippage_bps, False), impact)


def amm_accounts(user: str, pool: PoolView, base_program: str) -> list[dict]:
    quote = normalize_quote(pool.quote_mint)
    return [
        _meta(pool.pool, True),
        _meta(user, True, True),
        _meta(amm_global_pda(), False),
        _meta(pool.base_mint, False),
        _meta(quote, False),
        _meta(associated_token(user, pool.base_mint, base_program), True),
        _meta(associated_token(user, quote, TOKEN_PROGRAM), True),
        _meta(pool.pool_base, True),
        _meta(pool.pool_quote, True),
        _meta(base_program, False),
        _meta(TOKEN_PROGRAM, False),
        _meta(SYSTEM_PROGRAM, False),
        _meta(amm_user_volume_pda(user), True),
        _meta(fee_config_pda(PUMP_AMM_PROGRAM), False),
        _meta(pool.buyback, True),
        _meta(event_authority(PUMP_AMM_PROGRAM), False),
        _meta(PUMP_AMM_PROGRAM, False),
    ]


def build_amm(user: str, mint: MintView, pool: PoolView, tokens: int, side: str,
              slippage_bps: int | None, impact_cap_bps: int | None,
              blockhash: bytes, enabled: bool) -> Built:
    if not enabled:
        raise FailClosed("pumpswap_disabled")
    if pool.base_mint != mint.mint:
        raise FailClosed("mint_mismatch")
    if mint.program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise FailClosed("unknown_token_program")
    q = _amm_quote(pool, tokens, side, slippage_bps, impact_cap_bps)
    disc = BUY_V2 if side == "buy" else SELL_V2
    data = disc + _u64(tokens) + _u64(q.limit_atoms)
    accounts = amm_accounts(user, pool, mint.program)
    return Built("pumpswap_v2", side, data, accounts,
                 compile_message(user, accounts, data, blockhash), q)


def _compact(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return bytes([(n & 0x7F) | 0x80, n >> 7])
    raise FailClosed("compact_too_long")


def compile_message(payer: str, accounts: list[dict], data: bytes, blockhash: bytes) -> bytes:
    if not isinstance(blockhash, (bytes, bytearray)) or len(blockhash) != 32:
        raise FailClosed("blockhash_required")
    merged: dict[str, list] = {}

    def upsert(pubkey: str, signer: bool, writable: bool) -> None:
        slot = merged.get(pubkey)
        if slot is None:
            merged[pubkey] = [signer, writable]
        else:
            slot[0] = slot[0] or signer
            slot[1] = slot[1] or writable

    upsert(payer, True, True)
    program = accounts[-1]["pubkey"]
    upsert(program, False, False)
    for meta in accounts:
        upsert(meta["pubkey"], meta["signer"], meta["writable"])
    sign_w = [k for k in merged if merged[k][0] and merged[k][1]]
    sign_r = [k for k in merged if merged[k][0] and not merged[k][1]]
    rest_w = [k for k in merged if not merged[k][0] and merged[k][1]]
    rest_r = [k for k in merged if not merged[k][0] and not merged[k][1]]
    ordered = sign_w + sign_r + rest_w + rest_r
    index = {k: i for i, k in enumerate(ordered)}
    header = bytes([
        len(sign_w) + len(sign_r),
        len(sign_r),
        len(rest_r),
    ])
    body = b"".join(b58decode(k) for k in ordered)
    ix_accounts = bytes(index[m["pubkey"]] for m in accounts)
    ix = bytes([index[program]]) + _compact(len(ix_accounts)) + ix_accounts + _compact(len(data)) + data
    return header + _compact(len(ordered)) + body + bytes(blockhash) + _compact(1) + ix
