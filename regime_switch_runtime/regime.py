"""Walk-forward regime detection for DLMM LP strategy switching."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RegimeConfig:
    """Thresholds for the walk-forward regime classifier.

    The whipsaw, Hurst and momentum triggers are expressed as trailing
    percentiles or z-scores rather than absolute levels. An absolute bin-crossing
    count is not comparable across pools or candle intervals: a 4bps-bin pool on
    5m candles crosses ~90 bins per hour, while a 100bps-bin pool crosses a
    handful, so a fixed cutoff either fires always or never.
    """

    lookback: int = 96
    fast_lookback: int = 24
    # Baseline for trailing percentiles; 576 5m bars is ~2 days.
    percentile_lookback: int = 576
    atr_sigma_threshold: float = 2.5
    funding_z_threshold: float = 2.0
    momentum_z_threshold: float = 1.5
    whipsaw_pct_threshold: float = 0.90
    hurst_trend_pct_threshold: float = 0.85
    hurst_mean_revert_pct_threshold: float = 0.15


def add_regime_features(
    price_df: pd.DataFrame,
    funding_df: pd.DataFrame | None = None,
    *,
    bin_step_bps: float = 4.0,
    config: RegimeConfig | None = None,
) -> pd.DataFrame:
    cfg = config or RegimeConfig()
    df = price_df.copy().reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = df.get("close", 0.0)
        df[col] = pd.to_numeric(df[col], errors="coerce").ffill()
    df["ret"] = np.log(df["close"]).diff().fillna(0.0)
    df["atr"] = average_true_range(df, cfg.fast_lookback)
    df["atr_baseline"] = (
        df["atr"].rolling(cfg.lookback, min_periods=cfg.fast_lookback).mean()
    )
    df["atr_z"] = zscore(df["atr"], cfg.lookback)
    df["momentum"] = df["close"].pct_change(cfg.fast_lookback).fillna(0.0)
    df["hurst"] = rolling_hurst(df["close"], cfg.lookback)
    df["bin_crossings"] = rolling_bin_crossings(
        df["close"], bin_step_bps, cfg.fast_lookback
    )
    df["funding_rate"] = _align_funding(df, funding_df)
    df["funding_z"] = zscore(df["funding_rate"], cfg.lookback).fillna(0.0)
    df["whipsaw_pct"] = rolling_percentile(df["bin_crossings"], cfg.percentile_lookback)
    df["hurst_pct"] = rolling_percentile(df["hurst"], cfg.percentile_lookback)
    df["momentum_z"] = zscore(df["momentum"], cfg.percentile_lookback).fillna(0.0)
    dd_window = max(cfg.lookback * 3, cfg.fast_lookback)
    df["price_peak"] = (
        df["close"].rolling(dd_window, min_periods=cfg.fast_lookback).max()
    )
    df["price_dd"] = (
        (1.0 - df["close"] / df["price_peak"].replace(0, np.nan))
        .clip(lower=0.0)
        .fillna(0.0)
    )
    fast_ret = df["close"].pct_change(cfg.fast_lookback)
    df["price_dd_fast"] = (-fast_ret).clip(lower=0.0).fillna(0.0)
    df["regime"] = [classify_row(row, cfg) for _, row in df.iterrows()]
    return df


def classify_row(row: pd.Series, cfg: RegimeConfig | None = None) -> str:
    cfg = cfg or RegimeConfig()
    atr_z = float(row.get("atr_z", 0.0) or 0.0)
    funding_z = abs(float(row.get("funding_z", 0.0) or 0.0))
    whipsaw_pct = float(row.get("whipsaw_pct", 0.5) or 0.5)
    hurst_pct = float(row.get("hurst_pct", 0.5) or 0.5)
    momentum_z = float(row.get("momentum_z", 0.0) or 0.0)

    if funding_z >= cfg.funding_z_threshold or whipsaw_pct >= cfg.whipsaw_pct_threshold:
        return "risk_off_carry_quote"
    if (
        hurst_pct >= cfg.hurst_trend_pct_threshold
        and abs(momentum_z) >= cfg.momentum_z_threshold
    ):
        return "trend_up_one_sided" if momentum_z > 0 else "trend_down_one_sided"
    if (
        hurst_pct <= cfg.hurst_mean_revert_pct_threshold
        and atr_z < cfg.atr_sigma_threshold
    ):
        return "mean_revert_balanced"
    if atr_z >= cfg.atr_sigma_threshold:
        return "risk_off_carry_quote"
    return "balanced"


def average_true_range(df: pd.DataFrame, window: int) -> pd.Series:
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=max(2, window // 4)).mean().fillna(0.0)


def zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window, min_periods=max(5, window // 4)).mean()
    std = series.rolling(window, min_periods=max(5, window // 4)).std()
    return ((series - mean) / std.replace(0, np.nan)).fillna(0.0)


def rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """Trailing percentile rank of each value within the preceding window.

    Strictly causal: the current bar is ranked against earlier bars only, never
    against the full sample, so the classifier stays walk-forward.
    """
    s = pd.to_numeric(series, errors="coerce").ffill()
    min_periods = max(20, window // 8)
    ranks = s.rolling(window, min_periods=min_periods).apply(
        lambda w: float((w[:-1] <= w[-1]).mean()) if len(w) > 1 else 0.5,
        raw=True,
    )
    return ranks.fillna(0.5)


def rolling_hurst(series: pd.Series, window: int) -> pd.Series:
    values = (
        pd.to_numeric(series, errors="coerce").ffill().to_numpy(dtype=float)
    )
    out = np.full(len(values), 0.5)
    for i in range(window, len(values)):
        chunk = values[i - window : i]
        out[i] = hurst_exponent(chunk)
    return pd.Series(out, index=series.index)


def hurst_exponent(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    if len(values) < 20 or np.nanstd(values) == 0:
        return 0.5
    lags = np.arange(2, min(20, len(values) // 2))
    tau = []
    for lag in lags:
        diff = values[lag:] - values[:-lag]
        tau.append(np.sqrt(np.nanstd(diff)))
    tau = np.asarray(tau)
    valid = tau > 0
    if valid.sum() < 2:
        return 0.5
    slope, _ = np.polyfit(np.log(lags[valid]), np.log(tau[valid]), 1)
    return float(np.clip(slope * 2.0, 0.0, 1.0))


def rolling_bin_crossings(
    close: pd.Series, bin_step_bps: float, window: int
) -> pd.Series:
    price = pd.to_numeric(close, errors="coerce").ffill()
    step = max(bin_step_bps / 10_000.0, 1e-6)
    bins = np.floor(np.log(price / price.iloc[0]) / np.log(1 + step))
    crosses = pd.Series(bins).diff().abs().fillna(0.0)
    return crosses.rolling(window, min_periods=max(2, window // 4)).sum().fillna(0.0)


def _align_funding(df: pd.DataFrame, funding_df: pd.DataFrame | None) -> pd.Series:
    if funding_df is None or funding_df.empty:
        return pd.Series(0.0, index=df.index)
    f = funding_df.copy()
    if "timestamp" not in f.columns or "rate" not in f.columns:
        return pd.Series(0.0, index=df.index)
    f["timestamp"] = pd.to_numeric(f["timestamp"], errors="coerce")
    f["rate"] = pd.to_numeric(f["rate"], errors="coerce").fillna(0.0)
    f = f.dropna(subset=["timestamp"]).sort_values("timestamp")
    left = df[["timestamp"]].copy().sort_values("timestamp")
    aligned = pd.merge_asof(
        left, f[["timestamp", "rate"]], on="timestamp", direction="backward"
    )
    return aligned["rate"].fillna(0.0).reindex(df.index).fillna(0.0)
