---
name: lp_operator_playbook
description: Operate the Regime Switch LP strategy honestly — read this before answering
  questions about its state, tuning it, or explaining a pause/halt
when_to_use: Any time you're asked about this strategy's current state, whether to
  tune it, why it's paused/halted, or its historical performance — before answering
  from memory or guessing.
created: '2026-09-30T12:42:07Z'
source: agent:regime_switch_lp
---

The `regime_decision` routine is the only source of truth for regime/profile/action — never guess or recompute it yourself. Run it (or read `diagnose` for a reporting-only view) before answering any question about current state.

## Reading a decision
`profile` is one of `aggressive` (quiet/mean-reverting: full $520 LP allocation, default +/-0.7% range), `balanced` (ordinary ranging: full $520 LP allocation, default +/-1.3% range), or `paused` (LP off). Both active profiles default to a 100% hedge of measured LP SOL inventory, capped by the $260 1x hedge allocation. Every volatility, trend, funding, whipsaw, crash, missing/invalid-data, cooldown, recovery-confirmation, capital-halt, and failed economic-gate pause is non-overridable. The agent may evaluate evidence and explain when the routine has turned active again, but it may not force an early restart.

`halted` is a separate, stricter latch from the capital guard (dollar stops, drawdown, delta, basis, leverage sanity) — it is never overridable by tuning, and does not auto-clear. If you see it, explain the `halt_reason` plainly and say a human needs to review and explicitly clear it (`regime_decision` with `acknowledge_and_clear_halt=true` and a `clear_halt_rationale`) — do not suggest clearing it yourself without being asked, and never invent a rationale on the user's behalf.

## Tuning (advisory, bounded, and evidence-gated)
`online_adapt` is the only automatic self-modification path. It reviews completed, deduplicated hourly evidence every 12 hours and may write only an expiring proposal in the same frozen envelope. Never edit code, skills, tools, identities, or hard limits in response to live P&L. Never bypass or widen the economic gate.

1. Run `diagnose` first. Read its `attribution` and `fixed_rule_says` before proposing anything.
2. Only call `propose_tuning` if the diagnosis names a SPECIFIC driver and exact numeric evidence (e.g. hedge cost versus fees, out-of-range hours, delta, basis, volatility, or drawdown). Compare the proposed action with holding the current setting and name the incremental cost of rebuilding when relevant — never propose "to try something." The routine rejects rationales without a named diagnostic driver and a number.
3. Values must come from the frozen envelope: hedge_ratio ∈ {0.5, 0.8, 1.0}, capital_fraction ∈ {0, 1.0} (0 pauses; 1 deploys the full LP budget), range_width_pct ∈ {0.007, 0.01, 0.013}. A rationale is required and audited. Proposals are rejected inside a 12h window of the last different proposal and expire after 18h.
4. Prefer the cheapest change: hedge_ratio only re-hedges; a width change closes and rebuilds the LP, consuming a reposition slot. Never create LP positions more frequently than the 30-minute minimum.
5. A proposal never overrides `halted` or any volatility/data/recovery pause. `regime_decision` reports `tuning_status`; report that status, not your intent.
6. Never override the routine's economic gate. Entry/recenter requires conservatively expected fees to clear the configured multiple of conversion, fixed-action, divergence, and hedge costs.

## Honesty about performance
The source strategy's own backtests (232 simulations in the final short-history study plus 384 in the later overlapping retrospective, across base and stressed costs) showed an average loss in every realistic tested variant — there is no demonstrated edge. These are not 616 independent races. When reporting P&L, state it plainly from `get_performance_report`/`diagnose`'s attribution; do not claim the regime-switching is "working" just because a pause avoided a worse outcome, and do not editorialize toward optimism.

## Never
- Never call `create_lp_executor` / `create_order_executor` / `stop_executor` yourself outside of what the `Regime Operator` loop's own tick logic does — this agent's manual answers should read and explain, not trade ad hoc, unless the user explicitly asks you to take a one-off action and confirms it.
- Never invent a fill, balance, or P&L number not confirmed by `get_executor` / `get_portfolio_overview` / `get_performance_report`.
- Never treat an executor id as a position — an unfunded or rejected open terminates immediately; confirm volume filled.
