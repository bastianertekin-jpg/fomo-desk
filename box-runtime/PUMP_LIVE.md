# Pump.fun execution, disabled

This is construction against the sanitized desk source. It does not arm live trading, does not read a wallet, and does not send a transaction.

## What is implemented

- `pump_curve.py` builds official `buy_v3` / `sell_v3` instructions for program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, matching the public IDL and `@pump-fun/pump-sdk` 4.1.0. The quote is the bonding-curve formula with ceiling fees. Slippage is applied in basis points. The result is one legacy message containing that instruction. `partial_fill` is always off.
- Older bonding-curve accounts are accepted by zero-padding up to 166 bytes. A longer account with unexpected bytes is refused. Discovery does not filter on a single data size, so older curves are not dropped.
- `pump_exec.py` keeps orders in a separate `pump_orders.db`. It does not write the desk's coins, scans, positions, activity, balance, or settings. Statuses used: `rejected`, `built_unsigned`, `awaiting_confirm` (not written by this process), `confirmed_unfilled`, `filled`, `exit_failed`. The same intent is not built twice. A mint lock blocks a second buy. Three unfilled builds of one side stop further builds. Restart marks `awaiting_confirm` as held and does not retry or fill it. A fill requires a successful signature plus a token delta and a quote delta of the right signs.
- Exits call the existing `agents.risk.evaluate` (stop, trail, timeout). A sell that cannot be built becomes `exit_failed` and the mint stays locked. A filled sell releases the lock.
- `agents/pump_live.py` only reconciles that side file when it already exists, then sleeps. It does not heartbeat, so it does not appear as a sixth pipeline agent and health.py does not treat it as one. It does not call RPC.
- Telegram `live` and `LIVE` are refused, including a stale confirmation prompt. Other commands are unchanged. The CLI manual FOMO switch still asks for typed `LIVE` and still does not arm this path or sign anything.
- PumpSwap `buy_v2` / `sell_v2` for program `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` can be built only when the caller passes `pumpswap=True` for a migrated pool and supplies protocol, creator, and LP fee rates plus vault balances. The quote is constant product plus three ceiling fees. It is off otherwise.

The arm check is separate from `desk.mode`. All of these have to be true at once: caller is not Telegram, the typed text is exactly `LIVE`, a paper night is done, a review flag is set, the kill switch is explicitly false, a 32-byte wallet is present, the network is `mainnet-beta` or `devnet`, and every numeric limit is present: position, exposure, max positions, daily loss, quote age, slippage, price impact, fee reserve, fee cap. This build does not store those values and does not treat code defaults as approval. Even when a test supplies them, `DisabledSigner.sign` raises and the row stays `built_unsigned`.

## What is not implemented

- No RPC client call, no `getProgramAccounts` send, no blockhash fetch, no fee-account decode, no balance read, no signature, no broadcast, no confirmation watcher.
- The running agent never calls discovery or `prepare`. Curve rows are not published onto the scanner bus. `tradable` stays false. Legacy scanner filters and `DEFAULTS` are unchanged.
- Fee tiers are not decoded from the fee program. The caller must pass the protocol and creator rates. If the curve's own creator rate is nonzero and disagrees with the passed rate, the quote is refused.
- Cashback coins are refused (`buy_v3` / `sell_v3` do not support them). Mayhem, quote chains (`depth != 0`), non-SOL quotes, and a buy larger than the remaining curve supply are refused. There is no v2 curve fallback and no post-completion leg.
- The message does not add associated-token creation or a compute-budget instruction. A later broadcaster would still need those, a real blockhash, and a listed buyback recipient from global config.
- PumpSwap is not a guaranteed exit. Disable flags, empty or negative effective reserves, a missing fee rate, or the adapter flag being off all refuse.
- No numeric curve filter (age, market cap, or a replacement for liquidity / top 10 / h1) was approved, so none was invented. Those pool filters still apply only to the existing migrated-pool scanner.
- PumpPortal is not used as a new-token feed. The discovery body names that as unsupported and is not sent.
- Help text and the Account page still say to type LIVE in Telegram. The command now refuses. Those sentences were left so the UI text does not change.

## Tests

`tests/test_pump_live.py`: 16 passed. They cover the curve quote and account layout, structural rejects, Token-2022, disabled discovery, the disabled PumpSwap adapter, the arm rules, unsigned build plus signer refusal, idempotency, the mint lock, the three-build cap, restart hold, delta rules, risk exits, failed exits, and Telegram refusal. They do not open a network socket.

Full `pytest` in this tree: 131 passed, 62 failed, 7 errors, 2 skipped. 68 of the failures/errors are `FileNotFoundError` for `logs/raw` samples that were not in the sanitized archive (`rugcheck_report.json`, `rugcheck_insiders_graph_nonempty.json`, and the DexScreener samples those tests load through `tests/fakes.py`). Those fixtures were not invented. One pre-existing failure is `test_sw_never_caches_data_dir`: it expects cache name `desk-shell-v3-step13` while `web/sw.js` says `desk-shell-v4-homescreen`. The web files were not edited.

## Still required before any live transaction

A full PAPER night, a review of that night, user-approved numbers for every limit above, an approved wallet and network, and the user typing LIVE outside Telegram. A signer, RPC, fee-config decoding, and a decision about migrated-pool sends are still separate work. Nothing here restarts the desk or deploys it.
