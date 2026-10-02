"""Regime-aware LP/hedge decision engine for regime_switch_lp.

READ-ONLY + COMPUTE-ONLY: this routine never calls create_lp_executor,
create_order_executor, stop_executor, manage_clmm or any other capital-moving
tool. It reads market data + current on-chain/portfolio state and returns a
JSON decision for a separate loop to execute.
"""

CATEGORY = "Analysis"

import asyncio
import json
import logging
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
import numpy as np
import pandas as pd
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

POOL_NETWORK_DEFAULT = "solana-mainnet-beta"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
VENUE_MIN_ORDER_USD = 10.0
HEDGE_LOT_SIZE = 0.01
TUNING_HEDGE_RATIOS = (0.5, 0.8, 1.0)
TUNING_CAPITAL_FRACTIONS = (0.0, 1.0)
TUNING_RANGE_WIDTHS = (0.007, 0.01, 0.013)


class Config(BaseModel):
    """Regime-aware hedged LP decision: read market/portfolio state, return a JSON action plan (never executes)."""

    pool_address: str = Field(
        default="5rCf1DM8LjKTw4YqhnoLcngyZYeNnQqztScTogYHAS6",
        description="Meteora SOL-USDC CLMM pool address",
    )
    network: str = Field(default=POOL_NETWORK_DEFAULT, description="Gateway network")
    hedge_connector: str = Field(
        default="hyperliquid_perpetual", description="Perp connector used to hedge"
    )
    hedge_pair: str = Field(default="SOL-USD", description="Hedge trading pair")
    controller_id: str = Field(
        default="regime_switch_lp", description="Controller id tag for executors"
    )
    max_lp_usd: float = Field(default=520.0, description="Max LP budget (USD)")
    hedge_collateral_usd: float = Field(
        default=260.0, description="Max collateral/notional allotted to the 1x hedge (USD)"
    )
    position_loss_usd: float = Field(
        default=50.0, description="Stop loss since current position's entry (USD)"
    )
    daily_loss_usd: float = Field(default=75.0, description="UTC-day loss stop (USD)")
    total_loss_usd: float = Field(
        default=200.0, description="Loss stop since last halt-clear (USD)"
    )
    drawdown_usd: float = Field(default=200.0, description="Peak-to-now drawdown stop (USD)")
    max_net_delta_usd: float = Field(
        default=75.0, description="Max |net SOL delta| in USD before halting"
    )
    max_basis_bps: float = Field(
        default=100.0, description="Max |pool vs hedge price| basis in bps before halting"
    )
    max_openings_per_48h: int = Field(
        default=32, description="Aggregate LP creation cap per rolling 48h"
    )
    max_new_entries_per_48h: int = Field(
        default=8, description="Max entries/re-entries after being flat per rolling 48h"
    )
    max_repositions_per_48h: int = Field(
        default=24, description="Max in-pool recenter/rebuild creations per rolling 48h"
    )
    min_reposition_interval_seconds: int = Field(
        default=1800, description="Minimum time between LP creations"
    )
    max_position_age_seconds: int = Field(
        default=86400, description="Force a recenter once a position is this old"
    )
    max_feature_age_seconds: int = Field(
        default=7200,
        description="Fail closed when the completed hourly decision bar is older than this",
    )
    recenter_width_multiple: float = Field(
        default=1.0,
        description="Recenter when price reaches an LP range edge",
    )
    delta_recenter_trigger_usd: float = Field(
        default=60.0,
        description="Close/recenter before the hard residual-delta halt is reached",
    )
    economic_gate_enabled: bool = Field(
        default=True,
        description="Require conservative expected fee income to cover modeled deployment costs",
    )
    economic_horizon_hours: int = Field(default=12, description="Economic-gate forecast horizon")
    economic_cost_multiple: float = Field(
        default=1.5, description="Required expected-fee multiple over modeled costs"
    )
    fee_capture_efficiency: float = Field(
        default=0.25, description="Haircut applied to pro-rata pool fee estimates"
    )
    lp_conversion_cost_bps: float = Field(
        default=10.0, description="Modeled one-way LP conversion/slippage cost"
    )
    lp_fixed_action_cost_usd: float = Field(
        default=0.02, description="Modeled fixed cost for each LP create/close action"
    )
    hedge_all_in_cost_bps: float = Field(
        default=6.5, description="Modeled hedge fee plus impact for an opening adjustment"
    )
    acknowledge_and_clear_halt: bool = Field(
        default=False, description="Human-in-the-loop: clear a latched halt"
    )
    clear_halt_rationale: str = Field(
        default="", description="Required rationale to clear a halt"
    )


# ---------------------------------------------------------------------------
# Ported verbatim: regime classification (causal / walk-forward)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeConfig:
    lookback: int = 48
    fast_lookback: int = 2
    percentile_lookback: int = 48
    atr_sigma_threshold: float = 2.5
    funding_z_threshold: float = 2.0
    momentum_z_threshold: float = 1.5
    whipsaw_pct_threshold: float = 0.90
    hurst_trend_pct_threshold: float = 0.85
    hurst_mean_revert_pct_threshold: float = 0.15


def add_regime_features(price_df, funding_df=None, *, bin_step_bps=4.0, config=None):
    cfg = config or RegimeConfig()
    df = price_df.copy().reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = df.get("close", 0.0)
        df[col] = pd.to_numeric(df[col], errors="coerce").ffill()
    df["ret"] = np.log(df["close"]).diff().fillna(0.0)
    df["atr"] = average_true_range(df, cfg.fast_lookback)
    df["atr_z"] = zscore(df["atr"], cfg.lookback)
    df["momentum"] = df["close"].pct_change(cfg.fast_lookback).fillna(0.0)
    df["hurst"] = rolling_hurst(df["close"], cfg.lookback)
    df["bin_crossings"] = rolling_bin_crossings(df["close"], bin_step_bps, cfg.fast_lookback)
    df["funding_rate"] = _align_funding(df, funding_df)
    # A failed funding fetch comes back as NaN for every bar (see _align_funding) —
    # zscore() fillna's internally, so re-apply NaN on top for exactly those bars.
    # Otherwise a fetch failure would silently zscore to 0.0 and look neutral.
    funding_invalid = df["funding_rate"].isna()
    df["funding_z"] = zscore(df["funding_rate"], cfg.lookback)
    df.loc[funding_invalid, "funding_z"] = np.nan
    df["whipsaw_pct"] = rolling_percentile(df["bin_crossings"], cfg.percentile_lookback)
    df["hurst_pct"] = rolling_percentile(df["hurst"], cfg.percentile_lookback)
    df["momentum_z"] = zscore(df["momentum"], cfg.percentile_lookback).fillna(0.0)
    fast_ret = df["close"].pct_change(cfg.fast_lookback)
    df["price_dd_fast"] = (-fast_ret).clip(lower=0.0).fillna(0.0)
    df["feature_ready"] = df.index >= (cfg.lookback + cfg.percentile_lookback)
    df["regime"] = [classify_row(row, cfg) for _, row in df.iterrows()]
    return df


def classify_row(row, cfg=None):
    cfg = cfg or RegimeConfig()
    atr_z = float(row.get("atr_z", 0.0) or 0.0)
    funding_z = abs(float(row.get("funding_z", 0.0) or 0.0))
    whipsaw_pct = float(row.get("whipsaw_pct", 0.5) or 0.5)
    hurst_pct = float(row.get("hurst_pct", 0.5) or 0.5)
    momentum_z = float(row.get("momentum_z", 0.0) or 0.0)
    if funding_z >= cfg.funding_z_threshold or whipsaw_pct >= cfg.whipsaw_pct_threshold:
        return "risk_off_carry_quote"
    if hurst_pct >= cfg.hurst_trend_pct_threshold and abs(momentum_z) >= cfg.momentum_z_threshold:
        return "trend_up_one_sided" if momentum_z > 0 else "trend_down_one_sided"
    if hurst_pct <= cfg.hurst_mean_revert_pct_threshold and atr_z < cfg.atr_sigma_threshold:
        return "mean_revert_balanced"
    if atr_z >= cfg.atr_sigma_threshold:
        return "risk_off_carry_quote"
    return "balanced"


def average_true_range(df, window):
    high, low, close = (pd.to_numeric(df[c], errors="coerce") for c in ("high", "low", "close"))
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(window, min_periods=max(2, window // 4)).mean().fillna(0.0)


def zscore(series, window):
    mean = series.rolling(window, min_periods=max(5, window // 4)).mean()
    std = series.rolling(window, min_periods=max(5, window // 4)).std()
    return ((series - mean) / std.replace(0, np.nan)).fillna(0.0)


def rolling_percentile(series, window):
    s = pd.to_numeric(series, errors="coerce").ffill()
    min_periods = max(20, window // 8)
    return (
        s.rolling(window, min_periods=min_periods)
        .apply(lambda w: float((w[:-1] <= w[-1]).mean()) if len(w) > 1 else 0.5, raw=True)
        .fillna(0.5)
    )


def rolling_hurst(series, window):
    values = pd.to_numeric(series, errors="coerce").ffill().to_numpy(dtype=float)
    out = np.full(len(values), 0.5)
    for i in range(window, len(values)):
        out[i] = hurst_exponent(values[i - window : i])
    return pd.Series(out, index=series.index)


def hurst_exponent(values):
    values = np.asarray(values, dtype=float)
    if len(values) < 20 or np.nanstd(values) == 0:
        return 0.5
    lags = np.arange(2, min(20, len(values) // 2))
    tau = [np.sqrt(np.nanstd(values[lag:] - values[:-lag])) for lag in lags]
    tau = np.asarray(tau)
    valid = tau > 0
    if valid.sum() < 2:
        return 0.5
    slope, _ = np.polyfit(np.log(lags[valid]), np.log(tau[valid]), 1)
    return float(np.clip(slope * 2.0, 0.0, 1.0))


def rolling_bin_crossings(close, bin_step_bps, window):
    price = pd.to_numeric(close, errors="coerce").ffill()
    step = max(bin_step_bps / 10_000.0, 1e-6)
    bins = np.floor(np.log(price / price.iloc[0]) / np.log(1 + step))
    crosses = pd.Series(bins).diff().abs().fillna(0.0)
    return crosses.rolling(window, min_periods=max(2, window // 4)).sum().fillna(0.0)


def _align_funding(df, funding_df):
    if funding_df is None:
        # The Hyperliquid fetch failed outright (see _fetch_hl_funding) — fail
        # closed: every bar's funding is invalid (NaN), never a silent 0.0, so a
        # real funding-driven risk-off signal can't go undetected during an
        # HL outage. A successful-but-empty response (below) is a different,
        # genuinely-no-data case and still resolves to 0.0.
        return pd.Series(np.nan, index=df.index)
    if funding_df.empty:
        return pd.Series(0.0, index=df.index)
    f = funding_df.copy()
    if "timestamp" not in f.columns or "rate" not in f.columns:
        return pd.Series(0.0, index=df.index)
    f["timestamp"] = pd.to_numeric(f["timestamp"], errors="coerce").astype("float64")
    f["rate"] = pd.to_numeric(f["rate"], errors="coerce").fillna(0.0)
    f = f.dropna(subset=["timestamp"]).sort_values("timestamp")
    left = df[["timestamp"]].copy()
    left["timestamp"] = pd.to_numeric(left["timestamp"], errors="coerce").astype("float64")
    left = left.sort_values("timestamp")
    aligned = pd.merge_asof(left, f[["timestamp", "rate"]], on="timestamp", direction="backward")
    return aligned["rate"].fillna(0.0).reindex(df.index).fillna(0.0)


# ---------------------------------------------------------------------------
# Ported verbatim: operator state machine (hysteresis + kill-switch)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperatorDecision:
    lp_on: bool
    hedge_ratio: float
    range_width_pct: float
    capital_fraction: float
    profile: str
    reason: str
    regime: str


@dataclass
class OperatorState:
    last_profile: str = "balanced"
    bars_held: int = 0
    cooldown: int = 0
    peak_equity: float = 0.0
    last_bar_timestamp: float = 0.0
    recovery_bars: int = 0


@dataclass(frozen=True)
class OperatorConfig:
    min_hold_bars: int = 2
    recovery_confirm_bars: int = 2
    kill_drawdown: float = 0.05
    cooldown_bars: int = 8
    aggressive_hedge: float = 1.0
    balanced_hedge: float = 1.0
    aggressive_width: float = 0.007
    balanced_width: float = 0.013
    aggressive_capital_fraction: float = 1.0
    balanced_capital_fraction: float = 1.0
    atr_z_pause: float = 2.5
    hurst_trend_pct: float = 0.80
    momentum_z_pause: float = 1.25
    funding_z_pause: float = 2.5
    whipsaw_pct_pause: float = 0.90
    price_dd_pause: float = 0.04
    base_phi: float = 1e-3


def decide(row, *, equity, state: OperatorState, config: OperatorConfig):
    cfg = config
    required = ["atr_z", "hurst_pct", "momentum_z", "funding_z", "whipsaw_pct", "price_dd_fast"]
    bar_timestamp = float(row.get("timestamp") or 0.0)
    is_new_bar = bar_timestamp > state.last_bar_timestamp
    if is_new_bar:
        state.last_bar_timestamp = bar_timestamp
    invalid_reason = None
    observation_error = str(row.get("observation_error") or "").strip()
    if observation_error:
        invalid_reason = observation_error
    elif not math.isfinite(equity):
        invalid_reason = "invalid equity reading (not finite)"
    elif equity <= 0:
        invalid_reason = "non-positive equity reading"
    elif not bool(row.get("feature_ready", True)):
        # NOTE: previously compared with `is False`, which never matches a
        # numpy bool (different identity) and so never actually fired — this
        # warming-up branch was dead code. Cast explicitly instead.
        invalid_reason = "warming up: not enough bars yet for features"
    else:
        bad_fields = [k for k in required if not math.isfinite(float(row.get(k, 0.0)))]
        if bad_fields:
            invalid_reason = f"invalid observation(s): {', '.join(bad_fields)} not finite"
    if invalid_reason is not None:
        state.last_profile = "paused"
        state.recovery_bars = 0
        if is_new_bar:
            state.bars_held += 1
        return _paused(invalid_reason, "unavailable", cfg)
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
        if is_new_bar:
            state.cooldown -= 1
        proposed = _paused("cooldown after kill switch", classified, cfg)
    elif proposed.profile == "paused" and proposed.reason.startswith("kill switch:"):
        state.cooldown = cfg.cooldown_bars

    if proposed.profile == "paused":
        state.recovery_bars = 0
        if state.last_profile != "paused":
            state.last_profile = "paused"
            state.bars_held = 1
        elif is_new_bar:
            state.bars_held += 1
        return proposed

    # After volatility/data/risk pauses, require consecutive completed safe
    # hourly bars before reopening around the then-current price. Repeated
    # five-minute loop ticks on the same hourly bar do not count as recovery.
    if state.last_profile == "paused":
        if is_new_bar:
            state.recovery_bars += 1
        if state.recovery_bars < cfg.recovery_confirm_bars:
            return _paused(
                f"recovery confirmation {state.recovery_bars}/{cfg.recovery_confirm_bars}: stay flat",
                classified,
                cfg,
            )
        state.last_profile = proposed.profile
        state.bars_held = 1
        state.recovery_bars = 0
        return proposed

    if proposed.profile == state.last_profile:
        if is_new_bar:
            state.bars_held += 1
        return proposed
    if proposed.profile != "paused" and state.bars_held < cfg.min_hold_bars and state.last_profile:
        if is_new_bar:
            state.bars_held += 1
        return _from_profile(
            state.last_profile, f"hold {state.last_profile} ({state.bars_held} bars)", classified, cfg
        )
    state.last_profile = proposed.profile
    state.bars_held = 1
    return proposed


def _propose(*, classified, atr_z, hurst_pct, momentum_z, funding_z, whipsaw_pct, price_dd, equity_dd, cfg):
    # Specific, numeric pause conditions are checked BEFORE the generic
    # classifier-derived catch-all below, so the reported reason always names
    # the concrete metric that actually crossed its threshold (e.g. whipsaw)
    # instead of collapsing into a vague "classifier risk-off" message whenever
    # the classifier's own (independently-tuned) thresholds happen to agree.
    if equity_dd >= cfg.kill_drawdown:
        return _paused(f"kill switch: equity drawdown {equity_dd:.1%}", classified, cfg)
    if price_dd >= cfg.price_dd_pause:
        return _paused(f"crash in progress: 2h drop {price_dd:.1%}", classified, cfg)
    if atr_z >= cfg.atr_z_pause:
        return _paused(f"ATR expansion (atr_z={atr_z:.2f} >= {cfg.atr_z_pause:.2f}) — pause", classified, cfg)
    if hurst_pct >= cfg.hurst_trend_pct and abs(momentum_z) >= cfg.momentum_z_pause:
        side = "up" if momentum_z > 0 else "down"
        return _paused(f"trend_{side} — do not fade, pause DEX", classified, cfg)
    if funding_z >= cfg.funding_z_pause:
        return _paused(
            f"funding skew (funding_z={funding_z:.2f} >= {cfg.funding_z_pause:.2f}) — flatten perp, pause DEX",
            classified,
            cfg,
        )
    if whipsaw_pct >= cfg.whipsaw_pct_pause:
        return _paused(
            f"bin-crossing whipsaw (whipsaw_pct={whipsaw_pct:.1%} >= {cfg.whipsaw_pct_pause:.0%}) — pause DEX",
            classified,
            cfg,
        )
    if classified.startswith(("trend_", "risk_off")):
        return _paused(f"classifier risk-off ({classified}): pause DEX", classified, cfg)
    if hurst_pct <= 0.20 and atr_z < 1.0:
        return _from_profile("aggressive", "quiet/mean-reverting: full LP and hedge", classified, cfg)
    return _from_profile("balanced", "ranging: full LP allocation in wider range", classified, cfg)


def _paused(reason, regime, cfg):
    return OperatorDecision(
        lp_on=False,
        hedge_ratio=0.0,
        range_width_pct=cfg.balanced_width,
        capital_fraction=0.0,
        profile="paused",
        reason=reason,
        regime=regime,
    )


def _from_profile(profile, reason, regime, cfg):
    if profile == "aggressive":
        return OperatorDecision(
            lp_on=True,
            hedge_ratio=cfg.aggressive_hedge,
            range_width_pct=cfg.aggressive_width,
            capital_fraction=cfg.aggressive_capital_fraction,
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
        profile="balanced",
        reason=reason,
        regime=regime,
    )


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _state_path():
    from condor.paths import local_agents_root

    return local_agents_root() / "regime_switch_lp" / "state" / "regime_decision_state.json"


def _default_state() -> Dict[str, Any]:
    return {
        "operator": {
            "last_profile": "balanced",
            "bars_held": 0,
            "cooldown": 0,
            "peak_equity": 0.0,
            "last_bar_timestamp": 0.0,
            "recovery_bars": 0,
        },
        "guard": {
            "peak_equity": 0.0,
            "day_open_equity": 0.0,
            "last_equity": 0.0,
            "last_check_date": None,
            "halted": False,
            "halt_reason": None,
            "entry_equity": None,
            "opened_at": None,
            "since_halt_clear_equity": 0.0,
        },
        "lp_openings": [],
        "pending_open": None,
        "tracked_position": None,
        "hourly_history": [],
        "tuning": None,
        "audit": [],
    }


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Persistent strategy state is unreadable; refusing to trade until repaired: {e}"
        ) from e
    state = _default_state()
    for key in state:
        if key in raw:
            state[key] = raw[key]
    return state


def _save_state(state: Dict[str, Any]) -> None:
    from condor.fsutil import atomic_write_json

    atomic_write_json(_state_path(), state, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------


async def _fetch_pool_candles(pool_address: str, network: str) -> pd.DataFrame:
    from condor.pool_data import fetch_ohlcv

    rows, err = await fetch_ohlcv(
        pool_address, network, timeframe="1h", currency="usd", token="base", limit=160
    )
    if err or not rows:
        raise RuntimeError(f"Could not fetch pool candles: {err}")
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume", "_dt"])
    df = df.drop(columns=["_dt"]).sort_values("timestamp").reset_index(drop=True)
    return df


async def _fetch_hl_funding(coin: str, start_ms: int, end_ms: int) -> Optional[pd.DataFrame]:
    """Returns None when the fetch itself failed (network/HTTP error) — distinct
    from a successful call that legitimately found no funding events, which
    still returns an empty DataFrame. Callers must treat None as invalid data
    (fail closed), never silently substitute a neutral rate for it.
    """
    payload = {"type": "fundingHistory", "coin": coin, "startTime": start_ms, "endTime": end_ms}
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.post(HYPERLIQUID_INFO_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("regime_decision: hyperliquid funding fetch failed: %s", e)
        return None
    rows = []
    for item in data if isinstance(data, list) else []:
        try:
            rows.append(
                {"timestamp": float(item["time"]) / 1000.0, "rate": float(item["fundingRate"])}
            )
        except (KeyError, TypeError, ValueError):
            continue
    return pd.DataFrame(rows, columns=["timestamp", "rate"])


async def _fetch_pool_state(client, network: str, pool_address: str) -> Optional[Dict[str, Any]]:
    from condor.pool_data import fetch_pool_by_address

    return await fetch_pool_by_address(network, pool_address)


async def _fetch_tracked_lp_position(
    client, network: str, pool_address: str
) -> Optional[Dict[str, Any]]:
    """The single OPEN CLMM position on our pool, read from Condor's DB-cached
    position store (works even when the Gateway container is offline — unlike
    ``get_pool_info``/``get_positions_owned``, which call Gateway live).
    ``pool_address`` is this strategy's de-facto controller tag: no other
    controller in this install is expected to touch this specific pool.
    """
    try:
        resp = await client.gateway_clmm.search_positions(
            network=network, status="OPEN", limit=50
        )
    except Exception as e:
        logger.warning("regime_decision: search_positions failed: %s", e)
        return None
    rows = [r for r in resp.get("data", []) if r.get("pool_address") == pool_address]
    if not rows:
        return None
    if len(rows) > 1:
        raise RuntimeError(
            f"Ambiguous live state: {len(rows)} OPEN positions found on pool {pool_address}; "
            "quarantine and reconcile before trading"
        )
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows[0]


async def _fetch_hedge_position(client, connector: str, pair: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await client.trading.get_positions(connector_names=[connector])
    except Exception as e:
        logger.warning("regime_decision: get_positions failed: %s", e)
        return None
    for row in resp.get("data", []):
        if row.get("trading_pair") == pair:
            return row
    return None


async def _fetch_wallet_and_hedge_equity(
    client, network: str, hedge_connector: str
) -> Dict[str, float]:
    out = {
        "wallet_sol": 0.0,
        "wallet_usdc": 0.0,
        "hedge_account_usd": 0.0,
        "gateway_wallet_available": False,
    }
    try:
        state = await client.portfolio.get_state()
    except Exception as e:
        logger.warning("regime_decision: portfolio.get_state failed: %s", e)
        return out
    for account_data in state.values():
        for connector_name, tokens in account_data.items():
            is_gateway_wallet = "solana" in connector_name.lower() or connector_name == network
            for tok in tokens:
                symbol = tok.get("token")
                units = float(tok.get("units") or 0.0)
                value = float(tok.get("value") or 0.0)
                if is_gateway_wallet and symbol == "SOL":
                    out["wallet_sol"] += units
                    out["gateway_wallet_available"] = True
                elif is_gateway_wallet and symbol == "USDC":
                    out["wallet_usdc"] += units
                    out["gateway_wallet_available"] = True
                elif connector_name == hedge_connector:
                    out["hedge_account_usd"] += value
    return out


# ---------------------------------------------------------------------------
# Decision assembly
# ---------------------------------------------------------------------------


def _utc_date_str(ts: Optional[datetime] = None) -> str:
    return (ts or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _round_lot(units: float, lot: float = HEDGE_LOT_SIZE) -> float:
    return round(round(units / lot) * lot, 8)


def _economic_gate(
    candles: pd.DataFrame,
    pool: Dict[str, Any],
    *,
    budget_usd: float,
    width_pct: float,
    hedge_ratio: float,
    config: Config,
    is_recenter: bool,
) -> Dict[str, Any]:
    """Conservative all-in deploy/redeploy test.

    This is deliberately a veto, not a profit forecast. It uses only trailing
    observations, haircuts pro-rata fees, and charges conversion, fixed-action,
    divergence, and hedge costs before allowing an LP creation.
    """
    result: Dict[str, Any] = {
        "enabled": config.economic_gate_enabled,
        "passed": not config.economic_gate_enabled,
        "reason": "disabled" if not config.economic_gate_enabled else "insufficient inputs",
    }
    if not config.economic_gate_enabled:
        return result

    recent = candles.tail(24)
    volumes = (
        pd.to_numeric(recent["volume"], errors="coerce").dropna()
        if "volume" in recent.columns
        else pd.Series(dtype=float)
    )
    closes = (
        pd.to_numeric(recent["close"], errors="coerce").dropna()
        if "close" in recent.columns
        else pd.Series(dtype=float)
    )
    tvl_usd = float(
        pool.get("tvl_usd")
        or pool.get("liquidity_usd")
        or pool.get("total_value_locked_usd")
        or pool.get("reserve_usd")
        or 0.0
    )
    # Gateway exposes these fields as percentages (0.04 means 0.04%), while
    # the arithmetic below uses fractions.
    fee_pct = float(
        pool.get("base_fee_pct")
        or pool.get("base_fee_percentage")
        or pool.get("fee_pct")
        or 0.04
    ) / 100.0
    protocol_fee_pct = float(pool.get("protocol_fee_pct") or 0.0) / 100.0
    if budget_usd <= 0 or width_pct <= 0 or tvl_usd <= 0 or volumes.empty or len(closes) < 3:
        return result

    hourly_volume_usd = float(volumes.mean())
    returns = np.log(closes).diff().dropna()
    hourly_sigma = float(returns.std(ddof=0)) if not returns.empty else 0.0
    if not all(math.isfinite(v) for v in (hourly_volume_usd, hourly_sigma, tvl_usd, fee_pct)):
        return result

    liquidity_share = min(max(budget_usd / tvl_usd, 0.0), 0.05)
    expected_fees_usd = (
        hourly_volume_usd
        * config.economic_horizon_hours
        * max(fee_pct, 0.0)
        * (1.0 - min(max(protocol_fee_pct, 0.0), 1.0))
        * liquidity_share
        * config.fee_capture_efficiency
    )
    concentration = min(4.0, max(1.0, 0.013 / width_pct))
    divergence_cost_usd = (
        budget_usd * hourly_sigma * hourly_sigma * config.economic_horizon_hours * concentration / 8.0
    )
    # Price the complete holding cycle. A fresh entry includes open + eventual
    # exit; a recenter includes close-old + open-new + eventual exit-new.
    lp_action_count = 3 if is_recenter else 2
    lp_action_cost_usd = lp_action_count * (
        config.lp_fixed_action_cost_usd + budget_usd * config.lp_conversion_cost_bps / 10_000.0
    )
    hedge_cost_usd = budget_usd * 0.5 * hedge_ratio * config.hedge_all_in_cost_bps / 10_000.0
    modeled_cost_usd = divergence_cost_usd + lp_action_cost_usd + hedge_cost_usd
    required_fees_usd = config.economic_cost_multiple * modeled_cost_usd
    passed = expected_fees_usd >= required_fees_usd
    result.update(
        {
            "passed": passed,
            "reason": "expected fees cover conservative costs" if passed else "expected fees below conservative cost hurdle",
            "horizon_hours": config.economic_horizon_hours,
            "expected_fees_usd": round(expected_fees_usd, 6),
            "modeled_cost_usd": round(modeled_cost_usd, 6),
            "required_fees_usd": round(required_fees_usd, 6),
            "hourly_volume_usd": round(hourly_volume_usd, 2),
            "tvl_usd": round(tvl_usd, 2),
            "fee_pct": fee_pct,
            "hourly_sigma": round(hourly_sigma, 8),
        }
    )
    return result


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    from routines.base import RoutineResult
    from config_manager import get_client
    from condor.reports import ReportBuilder

    client = await get_client(context._chat_id, context=context)
    if not client:
        return "No server available"

    state = _load_state()
    op_cfg = OperatorConfig()
    reg_cfg = RegimeConfig()
    now = datetime.now(timezone.utc)
    diagnostics: Dict[str, Any] = {}

    # ---- 1. Market data -------------------------------------------------
    candles = await _fetch_pool_candles(config.pool_address, config.network)
    latest_ts_ms = int(candles["timestamp"].iloc[-1]) * 1000
    earliest_ts_ms = int(candles["timestamp"].iloc[0]) * 1000
    funding_df = await _fetch_hl_funding("SOL", earliest_ts_ms, latest_ts_ms + 3_600_000)

    features = add_regime_features(candles, funding_df, bin_step_bps=4.0, config=reg_cfg)
    # Drop the last (possibly still-forming) bar; use the second-to-last completed one.
    usable = features.iloc[:-1] if len(features) > 1 else features
    if usable.empty:
        raise RuntimeError("Not enough candle history to evaluate a bar")
    bar = usable.iloc[-1].copy()
    decision_bar_ts = float(bar.get("timestamp") or 0.0)
    feature_age_seconds = max(0.0, now.timestamp() - decision_bar_ts)
    if decision_bar_ts <= 0 or feature_age_seconds > config.max_feature_age_seconds:
        bar["observation_error"] = (
            f"missing/stale features: completed hourly bar is {feature_age_seconds:.0f}s old "
            f"(limit {config.max_feature_age_seconds}s)"
        )
    warmup_needed = reg_cfg.lookback + reg_cfg.percentile_lookback

    pool = await _fetch_pool_state(client, config.network, config.pool_address)
    if not pool:
        raise RuntimeError(f"Could not read live pool state for {config.pool_address}")
    pool_price = float(pool.get("current_price") or pool.get("base_token_price_usd") or 0.0)
    quote_price = float(pool.get("quote_token_price_usd") or 1.0)

    hedge_price_resp = await client.market_data.get_prices(config.hedge_connector, [config.hedge_pair])
    hedge_price = float(hedge_price_resp.get("prices", {}).get(config.hedge_pair) or 0.0)

    # ---- 2. Current state -------------------------------------------------
    lp_position = await _fetch_tracked_lp_position(client, config.network, config.pool_address)
    hedge_position = await _fetch_hedge_position(client, config.hedge_connector, config.hedge_pair)
    balances = await _fetch_wallet_and_hedge_equity(client, config.network, config.hedge_connector)

    hedge_side = (hedge_position or {}).get("side")
    hedge_amount = float((hedge_position or {}).get("amount") or 0.0)
    current_hedge_units = -hedge_amount if hedge_side == "SHORT" else hedge_amount
    hedge_leverage = (hedge_position or {}).get("leverage")

    lp_base_sol = float((lp_position or {}).get("base_token_amount") or 0.0)
    lp_quote_usdc = float((lp_position or {}).get("quote_token_amount") or 0.0)
    lp_lower = float((lp_position or {}).get("lower_price") or 0.0)
    lp_upper = float((lp_position or {}).get("upper_price") or 0.0)
    pnl_summary = (lp_position or {}).get("pnl_summary") or {}
    lp_total_value_quote = float(pnl_summary.get("current_total_value_quote") or 0.0)
    lp_value_usd = lp_total_value_quote * quote_price

    equity_usd = (
        balances["wallet_usdc"]
        + balances["wallet_sol"] * pool_price
        + lp_value_usd
        + balances["hedge_account_usd"]
    )

    # ---- 3. Position open/close bookkeeping (for the openings cap) --------
    tracked = state.get("tracked_position")
    lp_openings: List[Dict[str, Any]] = list(state.get("lp_openings") or [])
    pending_open = state.get("pending_open")

    if lp_position and (not tracked or tracked.get("position_address") != lp_position.get("position_address")):
        # A new position appeared since the last tick.
        kind = (pending_open or {}).get("kind", "new")
        lp_openings.append(
            {
                "created_at": lp_position.get("created_at") or now.isoformat(),
                "position_address": lp_position.get("position_address"),
                "kind": kind,
            }
        )
        state["tracked_position"] = {
            "position_address": lp_position.get("position_address"),
            "created_at": lp_position.get("created_at"),
        }
        state["pending_open"] = None
        pending_open = None
        if state["guard"]["entry_equity"] is None:
            state["guard"]["entry_equity"] = equity_usd
            state["guard"]["opened_at"] = lp_position.get("created_at") or now.isoformat()
    elif not lp_position and tracked:
        state["tracked_position"] = None
        state["guard"]["entry_equity"] = None
        state["guard"]["opened_at"] = None

    cutoff = now.timestamp() - 48 * 3600
    lp_openings = [
        o for o in lp_openings if _parse_iso_ts(o.get("created_at")) is not None and _parse_iso_ts(o["created_at"]) >= cutoff
    ]
    state["lp_openings"] = lp_openings
    # Separate re-entry from routine market-following. Both consume the
    # aggregate cap, while recentering also has its own cadence/churn limits.
    openings_last_48h = len(lp_openings)
    new_entries_last_48h = sum(1 for o in lp_openings if o.get("kind") == "new")
    repositions_last_48h = sum(1 for o in lp_openings if o.get("kind") == "recenter")
    opening_timestamps = [_parse_iso_ts(o.get("created_at")) for o in lp_openings]
    opening_timestamps = [ts for ts in opening_timestamps if ts is not None]
    seconds_since_last_open = (
        now.timestamp() - max(opening_timestamps) if opening_timestamps else float("inf")
    )

    # ---- 4. Capital guard ---------------------------------------------------
    guard = state["guard"]
    today = _utc_date_str(now)
    if guard["last_check_date"] != today:
        if guard["last_check_date"] is not None:
            guard["day_open_equity"] = guard["last_equity"]
        else:
            guard["day_open_equity"] = equity_usd
        guard["last_check_date"] = today
    if guard["peak_equity"] <= 0:
        guard["peak_equity"] = equity_usd
    guard["peak_equity"] = max(guard["peak_equity"], equity_usd)
    if guard["since_halt_clear_equity"] <= 0:
        guard["since_halt_clear_equity"] = equity_usd

    net_delta_sol = balances["wallet_sol"] + lp_base_sol + current_hedge_units
    net_delta_usd = abs(net_delta_sol) * pool_price
    basis_bps = abs(pool_price / hedge_price - 1.0) * 10_000 if hedge_price else 0.0
    hedge_notional = abs(current_hedge_units) * hedge_price

    new_halt_reason = None
    if not guard["halted"]:
        if guard["since_halt_clear_equity"] - equity_usd >= config.total_loss_usd:
            new_halt_reason = f"total loss >= ${config.total_loss_usd:.2f} since last halt-clear"
        elif guard["peak_equity"] - equity_usd >= config.drawdown_usd:
            new_halt_reason = f"peak drawdown >= ${config.drawdown_usd:.2f}"
        elif guard["day_open_equity"] - equity_usd >= config.daily_loss_usd:
            new_halt_reason = f"daily loss >= ${config.daily_loss_usd:.2f}"
        elif guard["entry_equity"] is not None and guard["entry_equity"] - equity_usd >= config.position_loss_usd:
            new_halt_reason = f"position loss >= ${config.position_loss_usd:.2f} since entry"
        elif net_delta_usd >= config.max_net_delta_usd:
            new_halt_reason = f"residual delta ${net_delta_usd:.2f} >= ${config.max_net_delta_usd:.2f}"
        elif basis_bps >= config.max_basis_bps:
            new_halt_reason = f"basis {basis_bps:.1f}bps >= {config.max_basis_bps:.1f}bps"
        elif hedge_notional > config.hedge_collateral_usd + 0.50:
            new_halt_reason = (
                f"hedge notional ${hedge_notional:.2f} exceeds collateral "
                f"${config.hedge_collateral_usd:.2f}+$0.50"
            )
        elif hedge_leverage is not None and float(hedge_leverage) > 1.0:
            new_halt_reason = f"hedge leverage {hedge_leverage}x > 1x"

    if new_halt_reason:
        guard["halted"] = True
        guard["halt_reason"] = new_halt_reason

    halt_clear_status = "not_requested"
    if config.acknowledge_and_clear_halt:
        if not config.clear_halt_rationale.strip():
            halt_clear_status = "refused"
        elif guard["halted"]:
            guard["halted"] = False
            guard["halt_reason"] = None
            guard["peak_equity"] = equity_usd
            guard["day_open_equity"] = equity_usd
            guard["since_halt_clear_equity"] = equity_usd
            if state["guard"]["entry_equity"] is not None:
                guard["entry_equity"] = equity_usd
            state.setdefault("audit", []).append(
                {
                    "ts": now.isoformat(),
                    "action": "halt_cleared",
                    "rationale": config.clear_halt_rationale,
                }
            )
            halt_clear_status = "cleared"
        else:
            halt_clear_status = "not_halted"

    guard["last_equity"] = equity_usd

    # ---- 5. Regime + operator decision -------------------------------------
    op_state = OperatorState(**state["operator"])
    previous_bar_timestamp = op_state.last_bar_timestamp
    op_decision = decide(bar, equity=equity_usd, state=op_state, config=op_cfg)
    state["operator"] = {
        "last_profile": op_state.last_profile,
        "bars_held": op_state.bars_held,
        "cooldown": op_state.cooldown,
        "peak_equity": op_state.peak_equity,
        "last_bar_timestamp": op_state.last_bar_timestamp,
        "recovery_bars": op_state.recovery_bars,
    }

    # ---- 5b. Agent tuning override (advisory, safety-gated) ---------------
    tuning_status: Optional[str] = None
    tuning = state.get("tuning")
    if tuning:
        expires_ts = _parse_iso_ts(tuning.get("expires_at"))
        tuning_values_valid = (
            float(tuning.get("hedge_ratio", -1)) in TUNING_HEDGE_RATIOS
            and float(tuning.get("capital_fraction", -1)) in TUNING_CAPITAL_FRACTIONS
            and float(tuning.get("range_width_pct", -1)) in TUNING_RANGE_WIDTHS
        )
        if not tuning_values_valid:
            tuning_status = "invalid_envelope_rejected"
            state.setdefault("audit", []).append(
                {"kind": "tuning_rejected", "at": now.isoformat(), "tuning": tuning}
            )
            state["tuning"] = None
        elif expires_ts is not None and now.timestamp() <= expires_ts:
            pause_prefixes = (
                "kill switch",
                "cooldown",
                "crash",
                "missing",
                "invalid",
                "warming",
                "ATR expansion",
                "trend_",
                "funding skew",
                "bin-crossing whipsaw",
                "classifier risk-off",
                "recovery confirmation",
            )
            is_non_overridable_pause = op_decision.profile == "paused" and any(
                op_decision.reason.startswith(p) for p in pause_prefixes
            )
            if guard["halted"]:
                tuning_status = "halted_not_overridable"
            elif is_non_overridable_pause:
                tuning_status = "safety_pause_not_overridable"
            else:
                was_pause = op_decision.profile == "paused"
                rationale = tuning.get("rationale", "")
                if float(tuning.get("capital_fraction", 0.0)) == 0.0:
                    op_decision = replace(
                        op_decision,
                        lp_on=False,
                        hedge_ratio=0.0,
                        capital_fraction=0.0,
                        profile="agent_pause",
                        reason=f"agent: {rationale}",
                    )
                else:
                    op_decision = replace(
                        op_decision,
                        lp_on=True,
                        hedge_ratio=float(tuning["hedge_ratio"]),
                        capital_fraction=float(tuning["capital_fraction"]),
                        range_width_pct=float(tuning["range_width_pct"]),
                        profile="agent_override" if was_pause else "agent_tuned",
                        reason=(
                            f"agent override of classifier pause: {rationale}"
                            if was_pause
                            else f"agent: {rationale}"
                        ),
                    )
                tuning_status = "applied"
                if tuning.get("applied_from") != tuning.get("at"):
                    tuning["applied_from"] = tuning.get("at")
                    state.setdefault("audit", []).append(
                        {"kind": "tuning_applied", "at": now.isoformat(), "tuning": tuning}
                    )
        else:
            tuning_status = "expired"
            state.setdefault("audit", []).append(
                {"kind": "tuning_expired", "at": now.isoformat(), "tuning": tuning}
            )
            state["tuning"] = None

    lp_on = op_decision.lp_on and not guard["halted"]

    # ---- 6. Actions ---------------------------------------------------------
    actions: List[Dict[str, Any]] = []
    reason = op_decision.reason
    economics: Optional[Dict[str, Any]] = None
    state["pending_open"] = pending_open  # may be cleared above

    if guard["halted"]:
        actions.append({"type": "notify", "message": f"HALTED: {guard['halt_reason']}"})
        reason = f"halted: {guard['halt_reason']}"
        # A capital halt is a flatten-all event. Close the LP first so the loop
        # can verify withdrawal, then remove the hedge; never leave naked SOL
        # exposure or preserve a risk position merely because the latch fired.
        if lp_position:
            actions.append({"type": "close_lp", "reason": guard["halt_reason"]})
        if abs(current_hedge_units) > 0:
            actions.append(_set_hedge_action(current_hedge_units, 0.0, hedge_price))
    else:
        half_width = (lp_upper - lp_lower) / 2.0 if lp_position else 0.0
        center = (lp_upper + lp_lower) / 2.0 if lp_position else 0.0
        drift = abs(pool_price - center) if lp_position else 0.0
        age_seconds = (now.timestamp() - _parse_iso_ts(lp_position["created_at"])) if lp_position and lp_position.get("created_at") else 0
        current_notional = lp_base_sol * pool_price + lp_quote_usdc
        target_notional = op_decision.capital_fraction * config.max_lp_usd
        capital_or_width_changed = lp_position is not None and (
            abs(current_notional - target_notional) > max(2.0, 0.15 * max(current_notional, target_notional))
            or (half_width > 0 and abs((half_width / center) - op_decision.range_width_pct) > 0.2 * op_decision.range_width_pct)
        )
        needs_recenter = lp_position is not None and (
            capital_or_width_changed
            or (half_width > 0 and drift >= config.recenter_width_multiple * half_width)
            or age_seconds >= config.max_position_age_seconds
            or net_delta_usd >= config.delta_recenter_trigger_usd
        )

        if lp_position and (not lp_on or needs_recenter):
            close_reason = "regime turned off DEX" if not lp_on else "recenter: " + (
                "position age" if age_seconds >= config.max_position_age_seconds
                else "price drifted off center" if drift >= config.recenter_width_multiple * half_width
                else "residual delta approached hard limit" if net_delta_usd >= config.delta_recenter_trigger_usd
                else "target capital/width changed"
            )
            actions.append({"type": "close_lp", "reason": close_reason})
            planned_base_sol = 0.0
            if lp_on and needs_recenter:
                recenter_block = None
                if openings_last_48h >= config.max_openings_per_48h:
                    recenter_block = (
                        f"aggregate opening cap {openings_last_48h}/{config.max_openings_per_48h}"
                    )
                elif repositions_last_48h >= config.max_repositions_per_48h:
                    recenter_block = (
                        f"reposition cap {repositions_last_48h}/{config.max_repositions_per_48h}"
                    )
                elif seconds_since_last_open < config.min_reposition_interval_seconds:
                    recenter_block = (
                        f"reposition cooldown {seconds_since_last_open:.0f}/"
                        f"{config.min_reposition_interval_seconds}s"
                    )
                economics = _economic_gate(
                    candles.iloc[:-1],
                    pool,
                    budget_usd=target_notional,
                    width_pct=op_decision.range_width_pct,
                    hedge_ratio=op_decision.hedge_ratio,
                    config=config,
                    is_recenter=True,
                )
                if not economics["passed"]:
                    recenter_block = (
                        "economic gate: expected fees $"
                        f"{economics.get('expected_fees_usd', 0.0):.4f} < required $"
                        f"{economics.get('required_fees_usd', 0.0):.4f}"
                    )
                if recenter_block:
                    reason = f"close and hold: recenter required but blocked by {recenter_block}"
                else:
                    budget = target_notional
                    planned_base_sol = (budget / 2.0) / pool_price if pool_price else 0.0
                    actions.append(
                        {
                            "type": "open_lp",
                            "lower_price": pool_price * (1 - op_decision.range_width_pct),
                            "upper_price": pool_price * (1 + op_decision.range_width_pct),
                            "budget_usd": budget,
                            "width_pct": op_decision.range_width_pct,
                        }
                    )
                    state["pending_open"] = {
                        "kind": "recenter",
                        "capital_fraction": op_decision.capital_fraction,
                        "width_pct": op_decision.range_width_pct,
                        "at": now.isoformat(),
                    }
            target_hedge_units = -min(planned_base_sol * op_decision.hedge_ratio, config.hedge_collateral_usd / hedge_price) if hedge_price else 0.0
            actions.append(_set_hedge_action(current_hedge_units, target_hedge_units, hedge_price))

        elif not lp_position and lp_on:
            entry_block = None
            economics = _economic_gate(
                candles.iloc[:-1],
                pool,
                budget_usd=target_notional,
                width_pct=op_decision.range_width_pct,
                hedge_ratio=op_decision.hedge_ratio,
                config=config,
                is_recenter=False,
            )
            if pending_open:
                entry_block = "unresolved prior open intent; reconcile before another submission"
            elif not economics["passed"]:
                entry_block = (
                    "economic gate: expected fees $"
                    f"{economics.get('expected_fees_usd', 0.0):.4f} < required $"
                    f"{economics.get('required_fees_usd', 0.0):.4f}"
                )
            elif openings_last_48h >= config.max_openings_per_48h:
                entry_block = f"aggregate opening cap {openings_last_48h}/{config.max_openings_per_48h}"
            elif new_entries_last_48h >= config.max_new_entries_per_48h:
                entry_block = f"entry cap {new_entries_last_48h}/{config.max_new_entries_per_48h}"
            elif seconds_since_last_open < config.min_reposition_interval_seconds:
                entry_block = (
                    f"entry cooldown {seconds_since_last_open:.0f}/"
                    f"{config.min_reposition_interval_seconds}s"
                )
            if entry_block:
                reason = f"hold: active regime but blocked by {entry_block}"
            else:
                budget = target_notional
                planned_base_sol = (budget / 2.0) / pool_price if pool_price else 0.0
                actions.append(
                    {
                        "type": "open_lp",
                        "lower_price": pool_price * (1 - op_decision.range_width_pct),
                        "upper_price": pool_price * (1 + op_decision.range_width_pct),
                        "budget_usd": budget,
                        "width_pct": op_decision.range_width_pct,
                    }
                )
                state["pending_open"] = {
                    "kind": "new",
                    "capital_fraction": op_decision.capital_fraction,
                    "width_pct": op_decision.range_width_pct,
                    "at": now.isoformat(),
                }
                target_hedge_units = -min(planned_base_sol * op_decision.hedge_ratio, config.hedge_collateral_usd / hedge_price) if hedge_price else 0.0
                actions.append(_set_hedge_action(current_hedge_units, target_hedge_units, hedge_price))

        elif lp_position and lp_on and not needs_recenter:
            # Position stays put; keep the hedge sized to its live base amount.
            target_hedge_units = -min(lp_base_sol * op_decision.hedge_ratio, config.hedge_collateral_usd / hedge_price) if hedge_price else 0.0
            if abs(target_hedge_units - current_hedge_units) * hedge_price >= VENUE_MIN_ORDER_USD:
                actions.append(_set_hedge_action(current_hedge_units, target_hedge_units, hedge_price))

        elif not lp_position and not lp_on and abs(current_hedge_units) > 0:
            actions.append(_set_hedge_action(current_hedge_units, 0.0, hedge_price))

        if not actions:
            reason = reason if reason.startswith("hold") else f"hold: {reason}"

    # ---- 7. Diagnostics + history ------------------------------------------
    diagnostics = {
        "pool_price": pool_price,
        "hedge_price": hedge_price,
        "atr_z": float(bar.get("atr_z", 0.0) or 0.0),
        "hurst_pct": float(bar.get("hurst_pct", 0.0) or 0.0),
        "momentum_z": float(bar.get("momentum_z", 0.0) or 0.0),
        "funding_z": float(bar.get("funding_z", 0.0) or 0.0),
        "whipsaw_pct": float(bar.get("whipsaw_pct", 0.0) or 0.0),
        "price_dd_fast": float(bar.get("price_dd_fast", 0.0) or 0.0),
        "feature_ready": bool(bar.get("feature_ready", False)),
        "feature_age_seconds": round(feature_age_seconds, 3),
        "warmup_bars_needed": warmup_needed,
        "bars_available": len(usable),
        "net_delta_usd": net_delta_usd,
        "basis_bps": basis_bps,
        "hedge_notional_usd": hedge_notional,
        "lp_base_sol": lp_base_sol,
        "lp_quote_usdc": lp_quote_usdc,
        "lp_value_usd": lp_value_usd,
        "current_hedge_units": current_hedge_units,
        "gateway_wallet_available": balances["gateway_wallet_available"],
        "delta_recenter_trigger_usd": config.delta_recenter_trigger_usd,
        "economic_gate": economics,
    }

    history = list(state.get("hourly_history") or [])
    in_range = (lp_lower <= pool_price <= lp_upper) if lp_position else None
    history.append(
        {
            "timestamp": now.isoformat(),
            "equity": equity_usd,
            "lp_value": lp_value_usd,
            "unclaimed_fees_usd": float(pnl_summary.get("total_fees_value_quote") or 0.0) * quote_price,
            "hedge_equity": balances["hedge_account_usd"],
            "hedge_units": current_hedge_units,
            "price": pool_price,
            "hedge_price": hedge_price,
            "lp_on": lp_position is not None,
            "in_range": in_range,
            "profile": op_decision.profile,
            "reason": reason,
            "regime": op_decision.regime,
            "halted": guard["halted"],
            "halt_reason": guard["halt_reason"],
            "hedge_ratio": op_decision.hedge_ratio,
            "capital_fraction": op_decision.capital_fraction,
            "range_width_pct": op_decision.range_width_pct,
        }
    )
    state["hourly_history"] = history[-72:]
    state.setdefault("tuning", None)

    if decision_bar_ts > previous_bar_timestamp:
        state.setdefault("audit", []).append(
            {
                "kind": "decision_receipt",
                "at": now.isoformat(),
                "decision_bar_timestamp": decision_bar_ts,
                "regime": op_decision.regime,
                "profile": op_decision.profile,
                "reason": reason,
                "actions": [a.get("type") for a in actions],
                "equity_usd": round(equity_usd, 4),
                "net_delta_usd": round(net_delta_usd, 4),
                "basis_bps": round(basis_bps, 4),
                "economic_gate": economics,
            }
        )
        state["audit"] = state["audit"][-200:]

    _save_state(state)

    decision = {
        "regime": op_decision.regime,
        "profile": op_decision.profile,
        "halted": guard["halted"],
        "halt_reason": guard["halt_reason"],
        "halt_clear_status": halt_clear_status,
        "actions": actions,
        "equity_usd": round(equity_usd, 4),
        "lp_openings_last_48h": openings_last_48h,
        "new_entries_last_48h": new_entries_last_48h,
        "repositions_last_48h": repositions_last_48h,
        "openings_cap": config.max_openings_per_48h,
        "reason": reason,
        "diagnostics": diagnostics,
    }
    if tuning_status is not None:
        decision["tuning_status"] = tuning_status

    # ---- 8. Report ------------------------------------------------------
    builder = ReportBuilder("Regime Switch LP — Decision")
    builder.source("routine", "regime_decision").tags(["regime_switch_lp", "lp", "hedge", "risk"])
    builder.section("01 / DECISION", "Read-only decision — a separate loop executes it")
    builder.kpi("Regime", op_decision.regime)
    builder.kpi("Profile", op_decision.profile)
    builder.kpi("Halted", "yes" if guard["halted"] else "no")
    builder.kpi("Equity", f"${equity_usd:,.2f}")
    builder.kpi("Actions", str(len(actions)))
    builder.kpi("Openings 48h", f"{openings_last_48h}/{config.max_openings_per_48h}")
    builder.kpi("Entries / Repositions", f"{new_entries_last_48h} / {repositions_last_48h}")
    builder.markdown(f"**Reason:** {reason}" + (f"\n\n**Halt reason:** {guard['halt_reason']}" if guard["halted"] else ""))
    if actions:
        builder.table(
            [{"type": a.get("type"), "detail": json.dumps({k: v for k, v in a.items() if k != "type"})} for a in actions],
            ["type", "detail"],
        )
    builder.section("02 / DIAGNOSTICS", "Feature values and risk figures used this tick")
    builder.table(
        [{"metric": k, "value": v} for k, v in diagnostics.items()],
        ["metric", "value"],
    )
    builder.manual_order()
    await builder.save()

    return RoutineResult(text=json.dumps(decision, allow_nan=False))


def _set_hedge_action(current_units: float, target_units: float, hedge_price: float) -> Dict[str, Any]:
    target_units = _round_lot(target_units)
    notional = abs(target_units - current_units) * hedge_price
    action = {
        "type": "set_hedge",
        "target_units": target_units,
        "current_units": round(current_units, 8),
        "reduce_only_floor_usd": VENUE_MIN_ORDER_USD,
    }
    if target_units == 0.0 and notional < VENUE_MIN_ORDER_USD:
        action["pad_to_venue_minimum"] = True
    return action


def _parse_iso_ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None
