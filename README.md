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

The installer validates every manifest hash and copies only the runtime package and four agent files. Existing overwritten files are backed up; unexpected legacy routines cause an error. It does not modify Condor core or API credentials. `requirements.txt` lists direct runtime dependencies and `requirements.lock.txt` records all 20 packages from the fresh standalone verification environment. Condor supplies its own routine framework and dependency lock; do not blindly replace its environment with the standalone lock. Latest Condor dependency installation is still required.

`operator_tick` is the only submitted routine and accepts **no parameters**. In Condor, select Regime Switch LP and run that routine with `{}`. With no REGIME_CONFIG_PATH it only checks public data. Do not add direct executor/order tools to the agent. Its configured LLM provider is an admin setting; the deterministic supervisor does not call an LLM.

## Account rehearsal and deployment

Botcamp must supply the assigned Hummingbot API account profile, Solana public wallet, Hyperliquid public address, both connectors, Solana RPC access, exact 48-hour UTC start/end, and confirmation of the $720/$40/$30/$10 capital partition. API credentials are delivered through a secure environment/secret store. **No personal deposit or private key in this archive/chat is needed.**

Copy config.example.json to a protected deployment path. Fill public account identifiers and UTC Unix-second timestamps; leave live=false for account diagnostics. The API connector wallets MUST match the public addresses in this file. `deployment_verified` records an admin's acceptance of these bindings, reserve segregation, installed backend capabilities and successful rehearsal; it is not automatic proof.

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

The $2/$8/$24 stops are exit triggers, not guaranteed loss caps. A $100 maximum loss cannot be promised. A verified, inaccessible $720 reserve and limited working wallets provide stronger protection than software stops alone, subject to custody and venue risks.
