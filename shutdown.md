---
on_kill_switch: flatten_all
---

# Regime Switch LP — shutdown policy

This strategy is delta-hedged: the Meteora SOL-USDC LP leg and the Hyperliquid SOL-USD
hedge only make sense together. The default `keep_spot_close_perp` policy is WRONG here —
closing only the hedge and leaving the LP open would strand naked, unhedged SOL exposure,
which is exactly what the hedge exists to prevent. Always close both legs together.

## On kill switch / emergency shutdown
1. Stop the LP executor (`stop_executor`, `keep_position=false`) for the Meteora SOL-USDC
   pool `5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6`. Confirm on-chain withdrawal via
   `get_executor` / `list_positions_held` before moving on — an LP close can leave a
   backend close-out swap still in flight after the on-chain withdrawal lands; don't race it.
2. If the LP executor terminated but a position is still on-chain (an orphan), do NOT open
   anything new. Follow the `recover_orphaned_position` skill: `manage_clmm(action="close", ...)`
   with the pool address, then `resolve_orphaned_position`.
3. Flatten the Hyperliquid SOL-USD hedge to zero (`create_order_executor`,
   `position_action="CLOSE"`, `execution_strategy="LIMIT"` at a marketable price, or `stop_executor`
   on its open order executor if one is still resting). If the required closing notional is
   under the venue's $10 minimum, pad the REDUCE-ONLY request quantity up to that floor —
   never pad an OPEN.
4. Do not reopen either leg after this — a kill switch means a human needs to review the
   `regime_decision` halt state (`guard.halted`, `guard.halt_reason`) before anything resumes.
5. `send_notification` summarizing what was closed, what (if anything) remains open or
   unverified, and the halt reason.
