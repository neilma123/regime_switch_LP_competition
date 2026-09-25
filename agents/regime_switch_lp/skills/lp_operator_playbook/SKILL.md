---
name: lp_operator_playbook
description: Operate the frozen Regime Switch LP routine and interpret its evidence honestly
---

Invoke only operator_tick with an empty configuration. Missing deployment configuration runs public checks. Treat SUBMITTED as an intent awaiting reconciliation, never as proof of a fill. WAITING_RECONCILIATION and HALTED_UNVERIFIED require continued automatic reconciliation, not duplicate orders or manual state resets. The deterministic supervisor handles exits and latches. Never modify immutable competition parameters or invent LP fee income or CEX quoting profit. Code, configuration and state must survive restarts. Live acceptance and account setup belong to Botcamp before the freeze; this candidate has not passed a funded rehearsal.
