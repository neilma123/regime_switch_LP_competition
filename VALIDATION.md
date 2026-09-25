# Competition readiness audit — September 24, 2026

## Decision

The selected strategy is **Regime Switch LP on Meteora + Hyperliquid**. The runtime package is suitable for admin review and controlled rehearsal. **It has not passed live-funded acceptance, and the available backtests do not demonstrate a profitable strategy.** Live remains disabled. No capital was moved, private wallet imported, order signed, or submission sent during this work.

The latest fetched Condor is `d89e74f2e3e273bea102c64e4977118e6a88084f`. A separate clean checkout at `../condor-admincheck` is used to validate the release without disturbing the user's WIP. The release includes its own runtime package and one operator routine, with no backtest files. It does not require this research fork's modifications to Condor's core risk gate or shutdown implementation.

## What was corrected

- Native Meteora observations replace inappropriate Orca/Gate paths and missing-data fallbacks in the competition loader. Exact timestamp joins require independent Hyperliquid prices and complete funding for each risk leg.
- Native fee-history boundary buckets were partial even when requested through the end second. Collection now requests an extra hour and discards that final bucket. Superseded datasets remain preserved; only `native_verified_20260924` is used for final comparisons.
- DLMM inventory uses discrete constant-sum bins, correct token-decimal prices and explicit bin limits. It remains an endpoint approximation, not swap replay. CJP has no implemented allocation-to-bin path, so it earns no artificial performance benefit.
- Initial hedging is immediate in simulation, both funding legs are charged when applicable, lots/minimum orders and total hedge exposure are constrained, closures incur costs, and final equity reconciles to attribution. No CEX quoting profit is invented.
- Balanced 70% capital previously still hit the same $40 cap. It now means $28. Classified trend/risk-off cannot be overwritten by another operator profile. Warmup pauses and future-data perturbation tests protect causality. The same policy function/config is imported by live and research.
- Independent account stops persist across restart/midnight/recovery. API failures during shutdown no longer mean an empty/flat account in the research Condor fork.
- Runtime intents are saved before submission. Lost responses are reconciled by ID, terminal partial fills unwind actual inventory, and pending uncertain hedges still trigger an owned-LP stop. LP withdrawal alone does not authorize a wallet sweep: its executor's close-out swap must finish first. If an owned LP survives a terminated executor, the runtime attempts its withdrawal directly, at most three times spaced by 180 seconds, with a fresh owned-position observation before each attempt. It never reports an unverified withdrawal as flat.
- The final preset caps openings at two per race. Earlier high-churn results are preserved and the change is recorded as a separate risk-budget revision, not a fitted profit improvement.

## Data and tests

Frozen native histories: `data/meteora_validation/native_verified_20260924/`, file hashes in the validation manifests. SOL-USDC and JUP-USDC have 719 joined hourly bars, August 25–September 24 UTC. USELESS-SOL has 382 usable bars because of hedge/funding coverage. Fees, price candles, independent hedge prices and funding are stored separately. Historical per-bin LP supply/trades are unavailable; current TVL and current universe introduce bias. USDC=$1 is an explicit assumption.

Thirty public live snapshots (10 per pair over roughly ten minutes) contain Cornell Gateway bin data and Hyperliquid L2. Maximum quoted spreads in that sample: SOL 1.727 bps, JUP 3.311 bps, USELESS 11.349 bps. A $30 static order-book impact calculation reached 5.674 bps for USELESS, so a generic 5-bps assumption is not conservative for every pair. These are observations, not fill/slippage guarantees. Median request latencies were roughly 1.7–2.2 seconds, maximum 6.65 seconds. Public live data and unsigned quote checks passed; no authenticated trading-account acceptance was performed.

Focused verification: 224 tests passed, covering simulator accounting/fees/limits/causality, runtime failure and restart handling, risk gates and shutdown error paths. Clean latest-Condor installation, routine discovery/import and public-data checks also passed. Condor discovery used the WSL environment; the standalone runtime also passed public checks in a fresh virtual environment containing only its 20 resolved runtime dependencies. Neither test is a clean Docker image build. Tests simulate broker failures; they do not establish production behavior of the deployed exchange/Gateway build.

## Paired 48-hour outcomes

`reports/meteora_validation/regime_30d_builderfee_20260925/` records **232 trials**: four variants, base/stress costs, 12 SOL windows, 12 JUP windows and five USELESS windows. All variants use common observed windows after feature warmup. First halves are development; later halves are reused historical validation, not untouched holdouts. No winning variant was selected on later-half returns.

| Pair | Windows | Always-on mean | Regime mean | Regime worst | Stress regime mean | Stress regime worst |
|---|---:|---:|---:|---:|---:|---:|
| SOL-USDC | 12 | -$1.39 | -$1.10 | -$1.33 | -$3.43 | -$3.62 |
| JUP-USDC | 12 | -$1.45 | -$1.06 | -$1.41 | -$3.32 | -$3.70 |
| USELESS-SOL | 5 | -$1.19 | -$1.36 | -$2.08 | -$3.68 | -$4.43 |

These results mostly show reduced activity and cost exposure. **Every base/stress candidate still loses.** They are not a prediction of next week's loss, confidence limits, proof of safety or evidence of a positive edge. The earlier `regime_addons_20260924_v1` kept four openings/day: regime stress mean losses were $12.42 SOL, $15.01 JUP and $10.98 USELESS. A strategy that exits and reopens too often can spend away its capital advantage.

Base costs: 5.5-bps all-in hedge fees (4.5-bps taker plus a conservative 1-bp potential approved builder fee), 5-bps assumed hedge impact, $0.15 LP action cost plus 30-bps conversion cost, 25% fee-capture haircut and no concentration multiplier. Stress: zero fee income, 20-bps hedge impact, $0.50 LP action plus 100-bps conversion cost. These are transparent scenarios, not receipt-calibrated costs. Rent is a capital requirement with measured refundable and nonrefundable components still to establish. Higher costs can sometimes make a losing strategy stop earlier and therefore appear to improve a path; this is not an economically optimal fee assumption.

The earlier broad `review_20260924_v3` contains 398 logged trials and selected cash in all 12 chronological forward folds. It used the prior opening policy and is not presented as the final runtime's exact performance. Older runs, all trials and hashes remain in their own folders. The final manifest includes hashes for both canonical runtime policy and research modules.

## September 25 extension: frozen policy on longer history

A new immutable acquisition, `data/meteora_validation/native_90d_20260925`, contains 2,160 requested hourly candles per pool, independent hedge prices and funding, with all 15 file hashes checked. No parameter was retuned for this extension. SOL has complete common history. JUP is excluded from this run because native candles are missing July 23 09:00 UTC and August 19 05:00 UTC; the earlier verified 30-day JUP study remains available. USELESS has only 403 funding events, so its usable common history is still roughly 17 days.

`reports/meteora_validation/regime_90d_builderfee_20260925` logs 384 additional simulations: 42 SOL and six USELESS 48-hour windows, four variants, two cost scenarios. This is a longer retrospective check with overlap against previous research, not a prospective holdout or 384 independent races.

| Frozen SOL policy | Windows | Mean P&L | Worst P&L | Best P&L |
|---|---:|---:|---:|---:|
| Always-on, base costs | 42 | -$1.37 | -$2.42 | -$0.57 |
| Regime, base costs | 42 | -$1.13 | -$1.62 | -$0.49 |
| Regime, stressed costs | 42 | -$3.37 | -$3.90 | -$1.64 |

Every SOL regime window still loses under these fee assumptions. Regime switching modestly reduces simulated loss relative to always-on, but does not establish an edge. The later-half volume-reference effect is -$0.006/race; the triple-barrier filter adds only $0.035/race and still worsens USELESS. Neither is promoted. Fee capture remains uncalibrated: in the earlier 30-day SOL regime run, average modeled LP income was $0.0034 against $0.972 LP action/conversion costs. This large gap makes real position-level fee and cost receipts essential; a conservative TVL proxy is not a measurement of actual concentrated LP earnings.

The runtime now detects retired executors with surviving owned LPs and attempts bounded direct withdrawal, preserving unknown status until on-chain confirmation. Malformed quotes and missing unclaimed-fee values fail closed. Final verification includes these regression tests. Public source Git checks on September 25 still resolve upstream Condor main to `d89e74f2e3e273bea102c64e4977118e6a88084f`.

## Add-on decisions

- **Volume-weighted reference:** a two-venue volume-weighted close, not true transaction VWAP. Final later-half paired effect versus regime: SOL -$0.002/race, JUP -$0.056, USELESS +$0.001. Too small/inconsistent and not executable price improvement evidence. Excluded.
- **CUSUM/triple-barrier prediction filter:** later-half paired effects SOL -$0.050, JUP +$0.107, USELESS -$0.289. Excluded. Dollar/time barriers remain independent execution risk controls.
- **Polymarket:** only four stored observations of two BTC year-end contracts across about 137 seconds, not an aligned historical SOL signal. Excluded; no fabricated backfill.
- **CJP bin allocation/learned policy:** no deployed mapping to actual Meteora bins; excluded. No fitted ML model is submitted.

## Statistical practice and the other audit

The supplied López de Prado book was extracted and consulted (chapters 7, 11–12 and 14). Overlapping outcome labels require purging; an embargo is tied to dependence/label horizon, not an automatic universal 1%. Trials and failed candidates must remain visible. Reused short samples, dependent market windows and survivorship/current-TVL bias prohibit strong generalization claims. No annualized Sharpe, deflated Sharpe or parameter sweep is called deployment proof. See [Bailey & López de Prado, Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

The other LLM's audit was checked against its scripts and outputs. Corrections: the roughly $435 loss was not all friction (its displayed LP/hedge charges were roughly $236); endpoint inventory was not exact DLMM replay; one-bar lag at later closes was not next-open execution; wider ranges exceeded connector limits; a $20 hedge minimum could prevent initial hedging; SOL-quoted exposure needed its own funding leg; its $24 stop already overshot in its own results. Candidate ordering after stop-trigger changes is not proof of optimality. No favorable narrative substitutes for consistent accounting and executable assumptions.

Hummingbot V2's inspected engine supports candle-based order/position/grid/DCA simulations, not LP replay. Condor report generation is not statistical validation. [Backtesting engine](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/backtesting/backtesting_engine_base.py).

## Execution facts and unresolved modeling

Meteora bins use constant-sum mechanics within bins; base plus variable fees and protocol shares require correct units. Metadata protocol share was 5% for the inspected pools; other documentation describes standard values, so no universal rate is asserted. Historical fee endpoint net/gross semantics remain uncertain; the budget sensitivity deducts protocol amounts conservatively and quarantines fee/volume mismatches. [Meteora formulas](https://docs.meteora.ag/core-products/dlmm/formulas), [fee collection](https://docs.meteora.ag/core-products/dlmm/collect-fee-mode), [volume API](https://docs.meteora.ag/api-reference/dlmm/pools/historical-volume).

Hyperliquid tier-zero taker/maker fees are 4.5/1.5 bps before account-specific adjustments; funding is hourly. A resting quote is not assumed to receive maker treatment or a fill. [Fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees), [funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding).

Solana base transaction fees and compute-unit priority fees are separate from account rent; receipts must establish the actual opening/closing costs and refunds. [Solana fees](https://solana.com/docs/core/fees). The inspected Gateway simple open path caps position width at 70 bins; this profile conservatively uses a 69-bin check. A successful unsigned wide quote does not prove an open transaction will work. The installed API schema explicitly says Meteora withdrawals do not enforce slippage_pct; that setting cannot guarantee a withdrawal price. Subsequent swaps have separate slippage controls. [Gateway Meteora implementation](https://github.com/hummingbot/gateway/blob/main/src/connectors/meteora/meteora.ts).

OrderExecutor stop does not unwind already filled quantity. The coordinator reconciles actual inventory and issues the required closing action. LP stop can include a backend swap after withdrawal, requiring terminal confirmation before our sweep. [Order executor](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/executors/order_executor/order_executor.py), [LP executor](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/strategy_v2/executors/lp_executor/lp_executor.py).

The simulator and runtime share the regime policy, not identical fill mechanics. Research executes at hourly endpoints; live uses fresh quotes, a 2% quote buffer, multiple confirmed steps, wallet gas inventory/rent, and a 15-second supervisor. The new supervisor can unwind 30 minutes before the deadline; hourly tests liquidate at the final endpoint. These differences require prospective receipt-based validation, not an assertion of exact live/backtest parity.

## What Botcamp must provide before arming

1. Assigned **Hummingbot API account profile name** (this was the meaning of “which account”), corresponding Solana public wallet and Hyperliquid public address, and securely provisioned connectors. No private keys in chat. Do not use someone else's Cornell account. Botcamp supplies/custodies funds under the user-provided rules.
2. Exact 48-hour UTC race start/end. The pasted rules list a multi-day finals window but do not identify this account's 48 hours.
3. Confirm $720 protected reserve plus $40 LP/$30 hedge/$10 operations is permitted and counted. Physically segregate it and prevent strategy credentials from moving the reserve. Confirm hedge collateral mode, no other positions/orders, and no external account flows during the race.
4. Reliable RPC/API access and exact pinned Hummingbot/Gateway versions. Verify account bindings, bin width, executor IDs, order minima, fixed slippage, cancellation and reduce-only closure on that build.
5. A funded pre-race rehearsal with receipts, followed by a full unattended 48-hour run covering restart, API failure, partial fill, low gas, exit slippage and scheduled unwind. Freeze only after acceptance. A current-data paper run is useful but does not replace signed execution tests.

Public collection does not need a wallet key. The existing 30-day dataset can support further diagnostics; actual fee/position modeling needs longer swap/bin histories and observed LP receipts. No access to private balances belonging to another Cornell account is required or appropriate.

Local Docker Desktop remains unavailable after its stale-socket failure. Earlier automatic approval review rejected the proposed Docker process/socket cleanup, so it was not performed. WSL native Condor and Cornell's existing services were used for research. A local Docker boot or finals Docker deployment has not been verified.

## Final connector-specific corrections

The inspected Hummingbot Hyperliquid connector adds up to **1 bp** for its Foundation builder route when that account previously approved it; otherwise the fee is zero. The latest runs conservatively include that extra basis point (5.5 bps total hedge fee). Earlier 4.5-bps runs are preserved. No builder approval was requested or signed. [Connector constants](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/derivative/hyperliquid_perpetual/hyperliquid_perpetual_constants.py), [Hyperliquid builder fee rules](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/builder-codes).

Hyperliquid limit prices require at most five significant figures as well as decimal/tick constraints. The runtime now rounds inward, so quantization does not widen the 20-bps price limit. [Tick and lot rules](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size).

Hummingbot's inspected generic order path rejects notional below $10 even before sending a CLOSE order. For a full residual hedge closure below that amount, the runtime raises only the **reduce-only request quantity** to the backend minimum, keeps position_action=CLOSE, and reconciles actual inventory to zero. It cannot report the requested padded amount as a fill. The deployed exchange/build's acceptance and clipping of this request is an explicit funded-rehearsal requirement; rejection remains unverified exposure. This adjustment must never be used for OPEN orders. [Hummingbot order validation](https://github.com/hummingbot/hummingbot/blob/master/hummingbot/connector/exchange_py_base.py), [Hyperliquid order types](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types).
