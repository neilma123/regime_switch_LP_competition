"""Dollar risk limits independent of the strategy, with a persisted halt latch.

These controls request exits; they cannot guarantee fill prices or venue uptime.
Physical funding limits and isolated collateral are required separately.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from .state import save as atomic_save


@dataclass(frozen=True)
class CapitalLimits:
    initial_equity: float = 800.0
    user_max_loss: float = 100.0
    max_lp_usd: float = 40.0
    hedge_collateral_usd: float = 30.0
    operating_reserve_usd: float = 10.0
    daily_loss_usd: float = 8.0
    total_loss_usd: float = 24.0
    drawdown_usd: float = 24.0
    position_loss_usd: float = 2.0
    max_net_delta_usd: float = 20.0
    max_basis_bps: float = 100.0
    max_data_age_seconds: float = 60.0
    max_failures: int = 3

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in asdict(self).values()):
            raise ValueError("All capital limits must be positive and finite")
        risk_capital = self.max_lp_usd + self.hedge_collateral_usd + self.operating_reserve_usd
        if risk_capital > self.user_max_loss or self.user_max_loss > self.initial_equity:
            raise ValueError("Working capital exceeds the user's loss budget")
        if max(self.total_loss_usd, self.drawdown_usd, self.daily_loss_usd) >= self.user_max_loss:
            raise ValueError("Stops must leave headroom below maximum acceptable loss")


@dataclass
class CapitalState:
    peak_equity: float = 800.0
    day: int = -1
    day_open_equity: float = 800.0
    last_equity: float = 800.0
    last_timestamp: int = -1
    halted: bool = False
    reason: str = ""

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_save(path, asdict(self))

    @classmethod
    def load(cls, path: Path):
        # Missing/corrupt live state is an error, never a fresh risk epoch.
        state = cls(**json.loads(path.read_text()))
        if not all(math.isfinite(x) and x > 0 for x in
                   [state.peak_equity, state.day_open_equity, state.last_equity]):
            raise ValueError("Corrupt capital state")
        if type(state.halted) is not bool:
            raise ValueError("Invalid halt latch")
        if not isinstance(state.reason, str) or (state.halted and not state.reason):
            raise ValueError("Invalid halt reason")
        if type(state.last_timestamp) is not int or state.last_timestamp < -1:
            raise ValueError("Invalid risk timestamp")
        return state


def check_capital(*, state: CapitalState, limits: CapitalLimits, timestamp: int,
                  equity: float, data_age_seconds: float = 0.0,
                  basis_bps: float = 0.0, reconciliation_ok: bool = True,
                  consecutive_failures: int = 0) -> str:
    """Return halt reason. No reset is permitted during a competition run."""
    if state.halted:
        return state.reason or "persisted capital halt"
    reason = ""
    if not all(math.isfinite(v) for v in [timestamp, equity, data_age_seconds, basis_bps]):
        reason = "invalid risk observation"
    elif timestamp < 0 or timestamp < state.last_timestamp:
        reason = "out-of-order risk observation"
    elif data_age_seconds < 0 or data_age_seconds > limits.max_data_age_seconds:
        reason = "stale market/account data"
    elif not reconciliation_ok:
        reason = "wallet/executor reconciliation failed"
    elif consecutive_failures >= limits.max_failures:
        reason = "repeated connector/transaction failures"
    else:
        day = int(timestamp) // 86400
        if state.day != day:
            # Include the loss across midnight instead of resetting it away.
            state.day_open_equity = state.last_equity
            state.day = day
        state.peak_equity = max(state.peak_equity, equity)
        if limits.initial_equity - equity >= limits.total_loss_usd:
            reason = "total dollar loss stop"
        elif state.peak_equity - equity >= limits.drawdown_usd:
            reason = "peak dollar drawdown stop"
        elif state.day_open_equity - equity >= limits.daily_loss_usd:
            reason = "daily dollar loss stop"
        elif abs(basis_bps) > limits.max_basis_bps:
            reason = "DEX/hedge basis limit"
        state.last_equity = equity
        state.last_timestamp = int(timestamp)
    if reason:
        state.halted, state.reason = True, reason
    return reason


def permit_new_exposure(*, state: CapitalState, limits: CapitalLimits,
                        current_lp_usd: float, pending_lp_usd: float,
                        proposed_lp_usd: float, hedge_notional_usd: float,
                        net_delta_usd: float, available_gas_usd: float,
                        live_ready: bool = False) -> bool:
    """Fail closed; reservations count so concurrent entries cannot overspend."""
    values = [current_lp_usd, pending_lp_usd, proposed_lp_usd,
              hedge_notional_usd, net_delta_usd, available_gas_usd]
    if state.halted or not live_ready or not all(math.isfinite(v) for v in values):
        return False
    if min(current_lp_usd, pending_lp_usd, proposed_lp_usd, hedge_notional_usd, available_gas_usd) < 0:
        return False
    return (current_lp_usd + pending_lp_usd + proposed_lp_usd <= limits.max_lp_usd
            and hedge_notional_usd <= limits.hedge_collateral_usd
            and abs(net_delta_usd) <= limits.max_net_delta_usd
            and available_gas_usd >= 2.0)
