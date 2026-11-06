"""Raw JSON key mapping from STEP 2 samples. Agents read only through this module."""
from __future__ import annotations

# PumpPortal migration message (logs/raw/pumpportal_migration.json)
PUMPPORTAL_MIGRATION = {
    "mint": "mint",
    "pool": "pool",  # sample value was "pump-amm" (label), not a base58 pool address
    "signature": "signature",
    "creator": None,  # not present in sample
    "tx_type": "txType",
}

# DexScreener tokens/v1/solana/{mint} list item (logs/raw/dexscreener_pair.json)
DEXSCREENER_PAIR = {
    "pair_address": "pairAddress",
    "dex_id": "dexId",
    "price_usd": "priceUsd",
    "liquidity_usd": ("liquidity", "usd"),
    "market_cap": "marketCap",
    "volume_m5": ("volume", "m5"),
    "volume_h1": ("volume", "h1"),
    "volume_h24": ("volume", "h24"),
    "txns_m5_buys": ("txns", "m5", "buys"),
    "txns_m5_sells": ("txns", "m5", "sells"),
    "txns_h1_buys": ("txns", "h1", "buys"),
    "txns_h1_sells": ("txns", "h1", "sells"),
    "price_change_m5": ("priceChange", "m5"),
    "price_change_h1": ("priceChange", "h1"),
    "price_change_h24": ("priceChange", "h24"),
    "pair_created_at": "pairCreatedAt",
    "name": ("baseToken", "name"),
    "symbol": ("baseToken", "symbol"),
    "mint": ("baseToken", "address"),
    # added from logs/raw/dexscreener_pair_one.json (observed keys)
    "chain_id": "chainId",
    "url": "url",
    "price_native": "priceNative",
    "fdv": "fdv",
    "quote_mint": ("quoteToken", "address"),
    "liquidity_base": ("liquidity", "base"),
    "liquidity_quote": ("liquidity", "quote"),
    "txns_h24_buys": ("txns", "h24", "buys"),
    "txns_h24_sells": ("txns", "h24", "sells"),
    "price_change_h6": ("priceChange", "h6"),
    "info": "info",
}

# DexScreener search response wrapper (logs/raw/dexscreener_search_pumpswap.json)
DEXSCREENER_SEARCH = {
    "pairs": "pairs",
}

# RugCheck report (logs/raw/rugcheck_report.json)
RUGCHECK_REPORT = {
    "creator": "creator",
    "creator_balance": "creatorBalance",
    "mint_authority": "mintAuthority",
    "freeze_authority": "freezeAuthority",
    "top_holders": "topHolders",
    "top_holder_pct": ("topHolders", "*", "pct"),
    "top_holder_owner": ("topHolders", "*", "owner"),
    "top_holder_address": ("topHolders", "*", "address"),
    "markets": "markets",
    "market_pubkey": ("markets", "*", "pubkey"),
    "market_type": ("markets", "*", "marketType"),
    "lp_locked_pct": ("markets", "*", "lp", "lpLockedPct"),
    "lp_burned_pct": None,  # no burned-% key in sample; only lpLockedPct
    # per-market (STEP 5 layer 5 reads the market whose pubkey == Scanner's pool)
    "m_pubkey": "pubkey",
    "m_type": "marketType",
    "m_lp_locked_pct": ("lp", "lpLockedPct"),
    "m_lp_base_usd": ("lp", "baseUSD"),
    "m_lp_quote_usd": ("lp", "quoteUSD"),
    "total_holders": "totalHolders",
    "token_program": "tokenProgram",
    # added from logs/raw/rugcheck_report.json (observed keys)
    "mint": "mint",
    "score": "score",
    "score_normalised": "score_normalised",
    "rugged": "rugged",
    "risks": "risks",
    "graph_insiders_detected": "graphInsidersDetected",
    "insider_networks": "insiderNetworks",
    "total_market_liquidity": "totalMarketLiquidity",
    "total_lp_providers": "totalLPProviders",
    "token_supply": ("token", "supply"),
    "token_decimals": ("token", "decimals"),
    "token_mint_authority": ("token", "mintAuthority"),
    "token_freeze_authority": ("token", "freezeAuthority"),
    "transfer_fee": "transferFee",
    "permanent_delegate": ("token_extensions", "permanentDelegate"),
    "non_transferable": ("token_extensions", "nonTransferable"),
    "top_holder_insider": ("topHolders", "*", "insider"),
    "lp_locked_usd": ("markets", "*", "lp", "lpLockedUSD"),
    "launchpad": "launchpad",
    "deploy_platform": "deployPlatform",
}

# RugCheck insiders graph. logs/raw/rugcheck_insiders_graph.json was [] (no networks);
# logs/raw/rugcheck_insiders_graph_nonempty.json (STEP 5, BONK) = list of networks:
# [{"net_id","network_type","nodes":[{"id","participant","holdings"}],"links":[{"source","target"}],"relatedMint"}]
RUGCHECK_INSIDERS_GRAPH = {
    "nodes": "nodes",
    "edges": "links",          # the graph calls its edges "links"
    "node_id": "id",
    "node_holdings": "holdings",
    "edge_source": "source",
    "edge_target": "target",
    "network_type": "network_type",
}

# RPC getAccountInfo mint jsonParsed (logs/raw/rpc_getAccountInfo.json)
RPC_MINT_ACCOUNT = {
    "mint_authority": ("result", "value", "data", "parsed", "info", "mintAuthority"),
    "freeze_authority": ("result", "value", "data", "parsed", "info", "freezeAuthority"),
    "supply": ("result", "value", "data", "parsed", "info", "supply"),
    "decimals": ("result", "value", "data", "parsed", "info", "decimals"),
}

# TypeSafe System One (Jev) response (logs/raw/jev_systemone_test.json)
JEV_SYSTEMONE = {
    "model": "model",
    "answers": "answers",
    "noul": "noul",          # per-answer key: answers.{q}.noul
    "input_tokens": ("usage", "input_tokens"),
    "output_tokens": ("usage", "output_tokens"),
}

# RPC getTokenLargestAccounts (pending sample if rate-limited)
RPC_TOKEN_LARGEST = {
    "address": ("result", "value", "*", "address"),
    "amount": ("result", "value", "*", "amount"),
    "ui_amount": ("result", "value", "*", "uiAmount"),
}

# RPC getTokenSupply
RPC_TOKEN_SUPPLY = {
    "amount": ("result", "value", "amount"),
    "decimals": ("result", "value", "decimals"),
    "ui_amount": ("result", "value", "uiAmount"),
}

# RPC token account jsonParsed (getTokenAccountsByOwner / getProgramAccounts / getMultipleAccounts item).
# Paths per Solana jsonParsed spl-token layout; rpc_gpa_*.json samples only stored counts — re-check on first live run.
RPC_TOKEN_ACCOUNT = {
    "owner": ("account", "data", "parsed", "info", "owner"),
    "delegate": ("account", "data", "parsed", "info", "delegate"),
    "delegated_amount": ("account", "data", "parsed", "info", "delegatedAmount", "uiAmount"),
    "token_amount": ("account", "data", "parsed", "info", "tokenAmount", "uiAmount"),
    "raw_amount": ("account", "data", "parsed", "info", "tokenAmount", "amount"),
    "state": ("account", "data", "parsed", "info", "state"),
}

# getMultipleAccounts jsonParsed item (no "account" wrapper)
RPC_PARSED_ACCOUNT = {
    "owner": ("data", "parsed", "info", "owner"),
}

# RPC getProgramAccounts Token / Token-2022 jsonParsed: list of {pubkey, account}
RPC_GPA = {
    "pubkey": "pubkey",
    "delegate": ("account", "data", "parsed", "info", "delegate"),
}


def dig(obj, path):
    """Resolve a dotted/tuple path; '*' means first list element for probing."""
    if path is None:
        return None
    if isinstance(path, str):
        path = (path,)
    cur = obj
    for part in path:
        if cur is None:
            return None
        if part == "*":
            if isinstance(cur, list) and cur:
                cur = cur[0]
            else:
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def get(obj, mapping: dict, key: str):
    """fields.get(raw, DEXSCREENER_PAIR, 'price_usd') -> value or None (never invents keys)."""
    return dig(obj, mapping.get(key))


def get_all(obj, path):
    """Like dig, but '*' fans out over every list element and returns a list."""
    if path is None:
        return []
    if isinstance(path, str):
        path = (path,)
    cur = [obj]
    for part in path:
        nxt = []
        for c in cur:
            if c is None:
                continue
            if part == "*":
                if isinstance(c, list):
                    nxt.extend(c)
            elif isinstance(c, dict):
                nxt.append(c.get(part))
        cur = nxt
    return [c for c in cur if c is not None]


def fnum(v):
    """Coerce DexScreener string numbers ("0.0001720") to float; None stays None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
