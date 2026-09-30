# Regime Switch LP — admin review package

This package fixes the missing-custom-package submission problem. Submit the **complete ZIP**, preserving folders. Do not upload the earlier loose files, research repository, private configuration, datasets, or backtests. The bundled `regime_switch_runtime` contains the shared policy and execution coordinator. No `lib_cjp`, Optuna, research solver, plotting package, or unpublished custom dependency is required by the runtime.

Status: runnable public diagnostics and a tested execution coordinator; **live entry disabled**. Historical tests remain unprofitable. A funded, signed, unattended execution rehearsal is outstanding. This archive is for code validation and that rehearsal, not a claim that production acceptance has passed. See strategy.md and VALIDATION.md.

## Install on current Condor

The clean compatibility checkout uses upstream Condor commit `d89e74f2e3e273bea102c64e4977118e6a88084f` (latest fetched September 24). Python 3.12+, Linux/WSL or Docker are required. The file lock uses Linux flock.

1. Use a clean current Condor checkout and install its dependencies (`uv sync`, as upstream documents). Do not merge this into an old folder full of strategy routines.
2. Extract this ZIP outside the checkout.
3. From the Condor root, run with its Python environment:

```bash
python /path/to/regime_switch_lp_release/install.py --condor-root .
python /path/to/regime_switch_lp_release/verify_install.py --public-check
python -m regime_switch_runtime --public-check
```

The installer validates every manifest hash and copies only the runtime package and six agent files. Existing overwritten files are backed up; unexpected legacy routines cause an error. It does not modify Condor core or API credentials. `requirements.txt` lists direct runtime dependencies and `requirements.lock.txt` records all 20 packages from the fresh standalone verification environment. Condor supplies its own routine framework and dependency lock; do not blindly replace its environment with the standalone lock. Latest Condor dependency installation is still required.

`operator_tick` is the execution routine and accepts **no parameters**. In Condor, select Regime Switch LP and run that routine with `{}`. With no REGIME_CONFIG_PATH it only checks public data. Do not add direct executor/order tools to the agent. Its configured LLM provider is an admin setting; the deterministic supervisor does not call an LLM.

### Advisory tuning (optional, `tuning_enabled`)

Two further routines let the Condor LLM agent diagnose losses and tune parameters on the fly **without** touching dollar caps, stops, the opening limit or the halt latch:

- `diagnose` (read-only): returns a packet built from the supervisor's persisted hourly history (last 24 h by default): net equity change, hedge equity change, unclaimed LP fees, hours with LP / in range, openings used, the rule's current decision, the active proposal and the envelope. It contains no credentials and nothing the supervisor has not already observed.
- `propose_tuning(hedge_ratio, capital_fraction, range_width_pct, rationale)`: writes `<state>.tuning.json`. Values must come from the frozen envelope (`regime_switch_runtime.tuning.COMPETITION_ENVELOPE`): hedge ratio {0.5, 0.8, 1.0}, capital fraction {0, 0.4, 0.7, 1.0} of the LP cap, half-width {0.7%, 1%, 1.3%}. A rationale is required; a new set of values is accepted at most every 12 h and expires after 18 h unless re-affirmed. Re-affirming the same values only refreshes the expiry.

The supervisor reads the file on every tick only when the frozen configuration has `tuning_enabled: true` (default false), re-validates it against the envelope, ignores expired or malformed files, and never lets a proposal override a kill switch, cooldown, crash or missing-data pause or the account halt. A proposal can pause the LP (fraction 0), keep it on through a classifier risk-off pause, change the hedge ratio (re-hedge only) or change fraction/width (close and reopen, consuming one of the two openings). Every application is written to `state.tuning` and the audit log. With `tuning_enabled: false` the routine refuses and the file is ignored, so the agent's availability is irrelevant to the supervisor. The paired offline evaluation of this loop against fixed-rule and always-on controls is described in VALIDATION.md; it does not show a profitable edge and the default remains off.

## Account rehearsal and deployment

Botcamp must supply the assigned Hummingbot API account profile, Solana public wallet, Hyperliquid public address, both connectors, Solana RPC access, exact 48-hour UTC start/end, and confirmation of the $720/$40/$30/$10 capital partition. API credentials are delivered through a secure environment/secret store. **No personal deposit or private key in this archive/chat is needed.**

Copy config.example.json to a protected deployment path. Fill public account identifiers and UTC Unix-second timestamps; leave live=false for account diagnostics. The API connector wallets MUST match the public addresses in this file. `deployment_verified` records an admin's acceptance of these bindings, reserve segregation, installed backend capabilities and successful rehearsal; it is not automatic proof.

### Reserve mode and wallet funding

`reserve_mode` selects how the non-working capital is accounted for. Both modes cap LP exposure at `max_lp_usd`, hedge notional at `hedge_collateral_usd`, and use the same $2/$8/$24 dollar stops; neither mode lets the strategy touch the reserve.

- `external` (default): the reserve is held outside the strategy wallets and added to equity as the attested constant `protected_reserve_usd`. The working Solana wallet must hold at most `max_lp_usd + operating_reserve_usd` (+$0.50) and the Hyperliquid account at most `hedge_collateral_usd` (+$0.50).
- `wallet_usdc`: the whole account stays in the strategy wallets as idle USDC. Equity is observed inventory only. At arming the Solana wallet may hold at most 1.5 × `entry_rent_and_gas_sol` as SOL (gas and rent); everything else must be USDC, so the reserve is never price exposed.

In both modes the first tick must observe equity within `equity_tolerance_usd` ($2) of `initial_equity_usd`, otherwise it refuses to create state. Fund so that `USDC + SOL × price + Hyperliquid account value = initial_equity_usd` at arming time, e.g. for the $800 `external` profile: 0.08 SOL, `40 − 0.08 × price + 10` USDC in the wallet and $30 on Hyperliquid; for `wallet_usdc`: 0.08 SOL, `770 − 0.08 × price` USDC and $30 on Hyperliquid. Only two capital partitions are accepted (`APPROVED_PROFILES` in settings.py): the $800 competition partition ($40 LP / $30 hedge / $10 operations / $720 reserve) and a $50 all-in-wallet rehearsal partition ($28 LP / $12 hedge / $10 operations / $0 reserve, `reserve_mode` `wallet_usdc`). Any other numbers are rejected.

Observation failures (RPC rate limit, late hourly candle, venue timeout) are retried on the next tick; three consecutive failures latch and stop the LP executor. Features are always one completed hourly bar old (3600–7200 s); ages above `feature_grace_seconds` pause new entries and ages above `max_feature_age_seconds` latch. An LP executor that fails to open with no position on chain is not a latch: the pre-acquired inventory is kept for one bounded retry after 60 s, the opening cap bounds retries, and a position that lands late is adopted only to be unwound.

Environment variables:

- REGIME_API_URL, REGIME_API_USERNAME, REGIME_API_PASSWORD: dedicated authenticated Hummingbot API.
- REGIME_SOLANA_RPC_URL: reliable confirmed-state Solana RPC (public endpoint fallback is suitable for diagnostics only).
- REGIME_CONFIG_PATH and REGIME_STATE_PATH: immutable account configuration and persistent race state file. Use separate dry-run/rehearsal/race state namespaces. Never delete or reset race state to bypass a halt.

```bash
python -m regime_switch_runtime --config /run/regime/config.json --state /state/race.json
python -m regime_switch_runtime --config /run/regime/config.json --state /state/race.json --loop
```

The second command is the independent risk supervisor (15 seconds between ticks plus network time). It continues after a halt and after the race deadline to reconcile exits. Keep it running under Docker/system supervision with durable state. `supervisor.example.yaml` is a deployment template; the image name and secret injection must be provided by Botcamp. Code/config should be read-only. Do not rely on an LLM or Telegram session to trigger stops.

Live arming requires live=true, deployment_verified=true, exact race interval and bound accounts. Do not arm simply to make a validation error disappear. Freeze configuration before the race. A configuration change with existing state is rejected. Starting with missing state and existing positions is rejected. There is no automatic state reset or exposure adoption.

Required rehearsal: actual acquisition -> LP deposit -> hedge -> regime pause -> LP withdrawal and backend swap completion -> reduce-only hedge close -> wallet sweep; verify fees, rent refunds, order minima (including sub-$10 full hedge closure through reduce-only quantity padding), approved builder fees, partial fills, orphaned LP withdrawal after backend termination, timeout/ambiguous response recovery, persistent restart, low-gas/stale-data shutdown, and scheduled final unwind. Observe balances and receipts, not merely HTTP success. Confirm the installed Hummingbot/Gateway build has the inspected reconciliation behavior and honors persisted executor IDs, fixed slippage, LP bin bounds and CLOSE order semantics. The Cornell development image is not a substitute for a pinned finals build.

The $2/$8/$24 stops are exit triggers, not guaranteed loss caps. A $100 maximum loss cannot be promised. A verified, inaccessible $720 reserve (`external` mode) and limited working wallets provide stronger protection than software stops alone, subject to custody and venue risks. In `wallet_usdc` mode the reserve is protected only by the exposure caps in this code and by the connector wallet's own key custody.
