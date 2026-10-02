"""Read-only attribution report for regime_switch_lp.

Reads the state persisted by `regime_decision` and reports what the fixed
rule currently says, the current position/guard state, and any pending
tuning proposal -- for a human or the agent's LLM to review before ever
acting on a tuning change. Never writes to regime_decision's state file.
"""

CATEGORY = "Analysis"

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

TUNING_ENVELOPE = {
    "hedge_ratio": [0.5, 0.8, 1.0],
    "capital_fraction": [0, 1.0],
    "range_width_pct": [0.007, 0.01, 0.013],
    "min_interval_hours": 12,
    "ttl_hours": 18,
    "frozen": "dollar caps, stops, entry/reposition caps, and any halt/volatility/data/recovery pause cannot be overridden by a proposal",
}


class Config(BaseModel):
    """Attribution report: what the fixed rule says now vs. recent equity/LP/hedge outcomes."""

    lookback_hours: int = Field(
        default=24, ge=1, le=72, description="Hours of hourly_history to attribute over"
    )


def _state_path():
    from condor.paths import local_agents_root

    return local_agents_root() / "regime_switch_lp" / "state" / "regime_decision_state.json"


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    from routines.base import RoutineResult
    from condor.reports import ReportBuilder

    path = _state_path()
    if not path.exists():
        packet: Dict[str, Any] = {
            "status": "NO_DEPLOYMENT",
            "detail": "regime_decision has not run yet; nothing to diagnose",
        }
        builder = ReportBuilder("Regime Switch LP — Diagnose")
        builder.source("routine", "diagnose").tags(["regime_switch_lp", "lp", "hedge", "risk"])
        builder.section("01 / STATUS", "No deployment found")
        builder.markdown("regime_decision has not run yet; nothing to diagnose.")
        builder.manual_order()
        await builder.save()
        return RoutineResult(text=json.dumps(packet, allow_nan=False))

    with open(path, "r", encoding="utf-8") as fh:
        state: Dict[str, Any] = json.load(fh)

    history: List[Dict[str, Any]] = list(state.get("hourly_history") or [])
    window = history[-config.lookback_hours :] if history else []

    if window:
        first, last = window[0], window[-1]
        first_price = float(first.get("price") or 0.0)
        last_price = float(last.get("price") or 0.0)
        attribution = {
            "hours_covered": len(window),
            "net_equity_change": last.get("equity", 0.0) - first.get("equity", 0.0),
            "hedge_equity_change": last.get("hedge_equity", 0.0) - first.get("hedge_equity", 0.0),
            "unclaimed_lp_fees_now_usd": last.get("unclaimed_fees_usd", 0.0),
            "hours_with_lp": sum(1 for r in window if r.get("lp_on")),
            "hours_in_range": sum(1 for r in window if r.get("in_range")),
            "lp_openings_in_window": len(state.get("lp_openings") or []),
            "price_change_pct": ((last_price / first_price - 1) * 100) if first_price else None,
        }
        latest = history[-1]
        fixed_rule_says = {
            "profile": latest.get("profile"),
            "reason": latest.get("reason"),
            "regime": latest.get("regime"),
            "hedge_ratio": latest.get("hedge_ratio"),
            "capital_fraction": latest.get("capital_fraction"),
            "range_width_pct": latest.get("range_width_pct"),
        }
    else:
        attribution = {
            "hours_covered": 0,
            "net_equity_change": None,
            "hedge_equity_change": None,
            "unclaimed_lp_fees_now_usd": None,
            "hours_with_lp": 0,
            "hours_in_range": 0,
            "lp_openings_in_window": len(state.get("lp_openings") or []),
            "price_change_pct": None,
        }
        fixed_rule_says = {
            "profile": None,
            "reason": None,
            "regime": None,
            "hedge_ratio": None,
            "capital_fraction": None,
            "range_width_pct": None,
        }

    guard = state.get("guard") or {}
    current_position = {
        "tracked_position": state.get("tracked_position"),
        "halted": guard.get("halted"),
        "halt_reason": guard.get("halt_reason"),
        "entry_equity": guard.get("entry_equity"),
        "opened_at": guard.get("opened_at"),
        "lp_openings_count": len(state.get("lp_openings") or []),
    }

    packet = {
        "status": "OK",
        "lookback_hours_requested": config.lookback_hours,
        "attribution": attribution,
        "fixed_rule_says": fixed_rule_says,
        "current_position": current_position,
        "active_proposal": state.get("tuning"),
        "tuning_envelope": TUNING_ENVELOPE,
    }

    builder = ReportBuilder("Regime Switch LP — Diagnose")
    builder.source("routine", "diagnose").tags(["regime_switch_lp", "lp", "hedge", "risk"])
    builder.section("01 / ATTRIBUTION", f"Last {attribution['hours_covered']}h vs. requested {config.lookback_hours}h")
    builder.kpi("Net Equity Change", f"${attribution['net_equity_change']:,.4f}" if attribution["net_equity_change"] is not None else "n/a")
    builder.kpi("Hedge Equity Change", f"${attribution['hedge_equity_change']:,.4f}" if attribution["hedge_equity_change"] is not None else "n/a")
    builder.kpi("Unclaimed LP Fees", f"${attribution['unclaimed_lp_fees_now_usd']:,.4f}" if attribution["unclaimed_lp_fees_now_usd"] is not None else "n/a")
    builder.kpi("Hours With LP", str(attribution["hours_with_lp"]))
    builder.kpi("Hours In Range", str(attribution["hours_in_range"]))
    builder.kpi("Price Change", f"{attribution['price_change_pct']:.2f}%" if attribution["price_change_pct"] is not None else "n/a")

    builder.section("02 / FIXED RULE", "What the current tick's rule output says right now")
    builder.table([{"field": k, "value": v} for k, v in fixed_rule_says.items()], ["field", "value"])

    builder.section("03 / CURRENT POSITION", "Tracked position, guard state, opening cap usage")
    builder.table([{"field": k, "value": v} for k, v in current_position.items()], ["field", "value"])

    builder.section("04 / TUNING", "Active proposal (if any) and the frozen envelope it must respect")
    builder.markdown(
        "**Active proposal:** " + (json.dumps(packet["active_proposal"]) if packet["active_proposal"] else "none")
    )
    builder.table([{"field": k, "value": json.dumps(v) if isinstance(v, list) else v} for k, v in TUNING_ENVELOPE.items()], ["field", "value"])

    builder.manual_order()
    await builder.save()

    return RoutineResult(text=json.dumps(packet, allow_nan=False))
