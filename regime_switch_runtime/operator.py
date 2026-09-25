"""Causal regime policy shared by research and the deterministic live coordinator.

OperatorConfig defaults preserve legacy experiments. Competition callers use the
explicit frozen profile from profile.regime_configs; no LLM chooses risk values.
"""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class OperatorDecision:
    lp_on: bool
    hedge_ratio: float
    range_width_pct: float
    capital_fraction: float
    phi: float
    profile: str
    reason: str
    regime: str


@dataclass
class OperatorState:
    last_profile: str = "balanced"
    bars_held: int = 0
    cooldown: int = 0
    peak_equity: float = 0.0


@dataclass(frozen=True)
class OperatorConfig:
    # From market_making_expert: pause rather than fade a trend.
    min_hold_bars: int = 12
    kill_drawdown: float = 0.04
    price_dd_pause: float = 0.04
    cooldown_bars: int = 96
    aggressive_hedge: float = 0.15
    balanced_hedge: float = 0.35
    aggressive_width: float = 0.04
    balanced_width: float = 0.12
    conservative_width: float = 0.20
    aggressive_capital_fraction: float = 1.0
    balanced_capital_fraction: float = 0.7
    conservative_capital_fraction: float = 0.4
    base_phi: float = 1e-3
    atr_z_pause: float = 2.5
    hurst_trend_pct: float = 0.80
    momentum_z_pause: float = 1.25
    funding_z_pause: float = 2.5
    whipsaw_pct_pause: float = 0.90


def decide(
    row,
    *,
    equity: float,
    state: OperatorState,
    config: OperatorConfig | None = None,
) -> OperatorDecision:
    """Causal operator tick. Mutates ``state`` (hold timer, cooldown, peak)."""

    cfg = config or OperatorConfig()
    required = ["atr_z", "hurst_pct", "momentum_z", "funding_z", "whipsaw_pct", "price_dd_fast"]
    if (not math.isfinite(equity) or equity <= 0 or row.get("feature_ready", True) == False
            or any(not math.isfinite(float(row.get(k, 0.0))) for k in required)):
        state.last_profile, state.bars_held = "paused", 1
        return _paused("missing, invalid or warming-up observations", "unavailable", cfg)
    if equity > state.peak_equity:
        state.peak_equity = equity
    peak = state.peak_equity if state.peak_equity > 0 else equity
    equity_dd = (peak - equity) / peak if peak else 0.0

    atr_z = float(row.get("atr_z", 0.0) or 0.0)
    hurst_pct = float(row.get("hurst_pct", 0.5) or 0.5)
    momentum_z = float(row.get("momentum_z", 0.0) or 0.0)
    funding_z = abs(float(row.get("funding_z", 0.0) or 0.0))
    whipsaw_pct = float(row.get("whipsaw_pct", 0.5) or 0.5)
    price_dd_fast = abs(float(row.get("price_dd_fast", 0.0) or 0.0))
    classified = str(row.get("regime", "balanced") or "balanced")

    proposed = _propose(
        classified=classified,
        atr_z=atr_z,
        hurst_pct=hurst_pct,
        momentum_z=momentum_z,
        funding_z=funding_z,
        whipsaw_pct=whipsaw_pct,
        price_dd=price_dd_fast,
        equity_dd=equity_dd,
        cfg=cfg,
    )

    if state.cooldown > 0:
        state.cooldown -= 1
        # Cooling down does not erase a realized drawdown or replenish risk budget.
        proposed = _paused("cooldown after kill switch", classified, cfg)
    elif proposed.profile == "paused" and proposed.reason.startswith("kill switch:"):
        state.cooldown = cfg.cooldown_bars

    if proposed.profile == state.last_profile:
        state.bars_held += 1
        return proposed

    # Hysteresis: stay in the current profile unless the new one is a pause
    # (safety always wins) or we have held long enough to trust the flip.
    if (
        proposed.profile != "paused"
        and state.bars_held < cfg.min_hold_bars
        and state.last_profile
    ):
        state.bars_held += 1
        return _from_profile(
            state.last_profile,
            f"hold {state.last_profile} ({state.bars_held} bars)",
            classified,
            cfg,
        )

    state.last_profile = proposed.profile
    state.bars_held = 1
    return proposed


def _propose(
    *,
    classified: str,
    atr_z: float,
    hurst_pct: float,
    momentum_z: float,
    funding_z: float,
    whipsaw_pct: float,
    price_dd: float,
    equity_dd: float,
    cfg: OperatorConfig,
) -> OperatorDecision:
    if equity_dd >= cfg.kill_drawdown:
        return _paused(f"kill switch: equity drawdown {equity_dd:.1%}", classified, cfg)
    if price_dd >= cfg.price_dd_pause:
        return _paused(f"crash in progress: 2h drop {price_dd:.1%}", classified, cfg)
    if classified.startswith(("trend_", "risk_off")):
        return _paused("classifier risk-off: pause DEX", classified, cfg)
    if atr_z >= cfg.atr_z_pause:
        return _paused("ATR expansion — MM playbook: pause", classified, cfg)
    if hurst_pct >= cfg.hurst_trend_pct and abs(momentum_z) >= cfg.momentum_z_pause:
        side = "up" if momentum_z > 0 else "down"
        return _paused(
            f"trend_{side} — MM playbook: do not fade, pause DEX", classified, cfg
        )
    if funding_z >= cfg.funding_z_pause:
        return _paused("funding skew — flatten perp, pause DEX", classified, cfg)
    if whipsaw_pct >= cfg.whipsaw_pct_pause:
        return _paused("bin-crossing whipsaw — pause DEX", classified, cfg)
    if hurst_pct <= 0.20 and atr_z < 1.0:
        return _from_profile(
            "aggressive", "quiet/mean-reverting: configured full LP and hedge", classified, cfg
        )
    return _from_profile("balanced", "ranging: configured reduced LP allocation and hedge", classified, cfg)


def _paused(reason: str, regime: str, cfg: OperatorConfig) -> OperatorDecision:
    return OperatorDecision(
        lp_on=False,
        hedge_ratio=0.0,
        range_width_pct=cfg.balanced_width,
        capital_fraction=0.0,
        phi=cfg.base_phi,
        profile="paused",
        reason=reason,
        regime=regime,
    )


def _from_profile(
    profile: str, reason: str, regime: str, cfg: OperatorConfig
) -> OperatorDecision:
    if profile == "aggressive":
        return OperatorDecision(
            lp_on=True,
            hedge_ratio=cfg.aggressive_hedge,
            range_width_pct=cfg.aggressive_width,
            capital_fraction=cfg.aggressive_capital_fraction,
            phi=cfg.base_phi * 0.5,
            profile=profile,
            reason=reason,
            regime=regime,
        )
    if profile == "conservative":
        return OperatorDecision(
            lp_on=True,
            hedge_ratio=1.0,
            range_width_pct=cfg.conservative_width,
            capital_fraction=cfg.conservative_capital_fraction,
            phi=cfg.base_phi * 3.0,
            profile=profile,
            reason=reason,
            regime=regime,
        )
    if profile == "paused":
        return _paused(reason, regime, cfg)
    return OperatorDecision(
        lp_on=True,
        hedge_ratio=cfg.balanced_hedge,
        range_width_pct=cfg.balanced_width,
        capital_fraction=cfg.balanced_capital_fraction,
        phi=cfg.base_phi,
        profile="balanced",
        reason=reason,
        regime=regime,
    )
