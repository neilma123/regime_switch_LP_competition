"""Frozen hourly policy shared by the simulator and runtime."""
def regime_configs():
    """Hourly features and holding periods, shared by validation and live code."""
    from .regime import RegimeConfig
    from .operator import OperatorConfig
    return (RegimeConfig(lookback=48, fast_lookback=2, percentile_lookback=48),
            OperatorConfig(min_hold_bars=2, kill_drawdown=.03, cooldown_bars=8,
                aggressive_hedge=.8, balanced_hedge=.8,
                aggressive_width=.01, balanced_width=.01, conservative_width=.01,
                aggressive_capital_fraction=1, balanced_capital_fraction=.7))

