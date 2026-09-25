---
name: Regime Switch LP
description: Frozen conservative Meteora/Hyperliquid candidate; live disarmed until funded acceptance
agent_key: null
skills:
- lp_operator_playbook
default_config:
  execution_mode: dry_run
  restart_on_boot: false
  frequency_sec: 60
  total_amount_quote: 800
---

Run `operator_tick` with no parameters and report its exact result. Do not reset state, tune risk limits or send direct trading orders. The separate supervisor owns execution and risk exits.

# Regime Switch LP — Meteora + Hyperliquid

Selected competition strategy, reviewed September 24, 2026. This is a runnable **review/rehearsal candidate**, with live entry disabled by default. Corrected historical tests do not establish a profitable edge. Do not interpret successful imports or simulated stop losses as funded readiness.

The agent supplies liquidity in Meteora SOL-USDC pool `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6`, with a matching SOL-USD perpetual hedge on Hyperliquid. SOL is selected for simpler single-base hedging and observed execution liquidity, not because it won a historical parameter search. JUP-USDC and USELESS-SOL remain research comparisons. There is no Gate/Orca execution, prediction-market trading, or invented CEX quoting P&L.

## Shared causal operator

`regime_switch_runtime.operator.decide` is the canonical policy. `lib_cjp.operator.decide` re-exports that exact function for research. `profile.regime_configs` is shared as well. The runtime requires 120 complete observed hourly candles plus hourly funding; features use 48-hour lookbacks and 48-hour trailing percentile windows, require 96-hour warmup, and act with one completed-bar delay. Hurst percentile, ATR z, momentum z, funding z, bin-crossing whipsaw percentile, two-hour close return and marked account drawdown determine the regime.

Quiet/mean reverting: aggressive, up to $40 LP. Balanced: 70% of that reservation, up to $28. Both hedge 80% of observed LP SOL, subject to $30 aggregate notional and venue size rules. Research fees include 4.5-bps taker plus a conservative 1-bp possible approved builder fee. The quote request leaves a 2% reservation buffer. Range is +/-1%, with 150-bps recenter threshold. Current simple Gateway opening width must fit 69 bins; wide +/-12% ranges on this 4-bps pool are excluded.

Classified trend/risk-off, ATR z >=2.5, absolute funding z >=2.5, whipsaw percentile >=90%, a two-hour drop >=4%, or equity drawdown >=3% pauses LP. Trend detection also uses Hurst/momentum. Safety pauses bypass the two-bar profile hold. Equity kills begin an eight-hour operator cooldown, while the independent account halt stays latched for the entire race. No reset replenishes the loss budget.

## Budget and exits

Botcamp must segregate the $800 before arming: $40 LP allocation, $30 Hyperliquid collateral, $10 SOL/rent/transaction reserve, and $720 protected outside wallets accessible to the strategy. These transfers have not been performed. Configuration alone does not segregate capital. The $720 is an attested fixed reserve in accounting; Botcamp must confirm this partition still counts toward the competition account.

Stops request exits at $2 account loss since LP entry, $8 UTC-day loss, $24 total loss or peak drawdown, $20 residual SOL delta, 100-bps DEX/perp basis, stale/invalid data, unexpected account exposure or inadequate gas. Maximum LP age is 24 hours and there are at most two openings for the whole 48-hour race. Hedge leverage is at most 1x; executable quantities are rounded to current SOL rules (0.01 SOL, $10 new-order minimum). There is no dollar-loss guarantee: gaps, outages, failed exits and stablecoin risk can exceed triggers. The physical $80 working-capital partition is the main protection against the user's $100 loss tolerance, conditional on correct custody and venue setup.

The deterministic supervisor checks every 15 seconds plus network latency. Every intent is saved atomically before submission under a process lock. Unknown outcomes are reconciled, not blindly resubmitted. Terminal partial fills latch the halt and unwind actual observed inventory. On pause, stop the owned LP, verify on-chain withdrawal AND completion of its backend close-out swap, then flatten the hedge and sweep residual acquired SOL. If the LP executor terminates with owned liquidity still on-chain, attempt direct withdrawal with fresh ownership checks, capped at three attempts spaced by 180 seconds; unresolved exits stay unverified. Keep the initial SOL gas buffer. End-of-race unwinding starts 30 minutes before the configured end; the process continues monitoring afterward.

The LLM can explain observations and invoke the fixed routine; it cannot supply equity, position sizes, stop settings, account identifiers or state resets. Account configuration and code are read-only during the race. Run the deterministic supervisor independently of LLM availability, with persistent state and automatic process restart. Latest Condor compatibility is checked in a clean upstream checkout; no modified Condor risk-gate package is required by this release.

## Add-ons and evidence

Excluded: Polymarket (only four historic signal records, no aligned validation), volume-weighted reference (inconsistent paired improvement), CUSUM/triple-barrier prediction filter (not generalizable across these pairs), and CJP bin-shape optimization (not implemented in the executable allocation). Dollar/time exit barriers remain as risk controls. No trained model is deployed.

The final two-opening research preset was evaluated in 232 logged trials on common chronological 48-hour windows: 12 each for SOL/JUP, five for USELESS. The SOL regime mean was -$1.10, worst -$1.33; under the zero-fee/high-cost stress it was -$3.43, worst -$3.62. These are descriptive simulated outcomes from reused, short history, not forecasts or bounds. All tested candidates still lost after costs. The earlier wider research comparison selected cash in all 12 forward folds. A September 25 extension with frozen parameters produced 42 SOL race windows from 90 days of history: regime mean -$1.13, worst -$1.62; stressed mean -$3.37, worst -$3.90. All remained negative. JUP was rejected in the longer run for two missing native candles; USELESS still had only about 17 days of matching funding. These observations do not justify promoting an add-on or claiming profitability.

Fees use native historical volume plus conservative fee-share scenarios; no historical per-bin supply or swap traversal is available. Current TVL, USDC=$1, endpoint bin approximation, immediate simulated fills versus multi-step live execution, entry quote buffering, network costs and exit latency remain important limitations. Actual receipts, loss-trigger overshoot and a full unattended rehearsal are still required. See VALIDATION.md in the release.

## Operation

Install the complete release archive using its README, not a collection of loose Python files. The release includes its own runtime package and excludes backtests. Without account configuration, `operator_tick` runs public diagnostics only. `python -m regime_switch_runtime --public-check` needs no wallet keys. An account rehearsal additionally needs Botcamp's account profile, public wallet addresses, RPC and API access, fixed race timestamps, and securely provisioned connector credentials. Do not paste private keys into chat or commit them.
