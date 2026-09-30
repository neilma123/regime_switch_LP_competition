---
name: lp_operator_playbook
description: Operate the frozen Regime Switch LP routine and interpret its evidence honestly
---

Invoke operator_tick with an empty configuration. Missing deployment configuration runs public checks. Use diagnose before any propose_tuning call and cite one attribution line (hedge, LP vs hold, fees, costs, out-of-range hours) in the rationale. Hold when the last day shows no LP exposure or a safety pause; do not spend an opening to "try something". Proposals are advisory, envelope-bounded, expire after eighteen hours, and are refused when tuning_enabled is false. Treat SUBMITTED as an intent awaiting reconciliation, never as proof of a fill. WAITING_RECONCILIATION and HALTED_UNVERIFIED require continued automatic reconciliation, not duplicate orders or manual state resets. The deterministic supervisor handles exits and latches. Never modify immutable competition parameters or invent LP fee income or CEX quoting profit. Code, configuration and state must survive restarts. Live acceptance and account setup belong to Botcamp before the freeze; this candidate has not passed a funded rehearsal.
