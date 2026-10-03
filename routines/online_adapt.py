"""Bounded online policy adaptation for regime_switch_lp.

This routine never changes code, hard risk limits, pool identity, or execution
permissions. Every 12 completed hours it may write one expiring proposal inside
the existing hedge/capital/width envelope. The deterministic regime, economic
gate, and capital guard remain authoritative.
"""

CATEGORY = "Analysis"

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

HEDGE_RATIOS = (0.5, 0.8, 1.0)
CAPITAL_FRACTIONS = (0.0, 1.0)
RANGE_WIDTHS = (0.007, 0.01, 0.013)
PROPOSAL_TTL = timedelta(hours=18)


class Config(BaseModel):
    """Review realized evidence and adapt only inside the frozen policy envelope."""

    enabled: bool = Field(default=True)
    review_hours: int = Field(default=12, ge=6, le=24)
    lookback_hours: int = Field(default=24, ge=12, le=72)
    loss_pause_usd: float = Field(default=10.0, ge=0.0)
    fee_cost_multiple: float = Field(default=1.5, ge=1.0, le=3.0)


def _state_path():
    from condor.paths import local_agents_root
    return local_agents_root() / "regime_switch_lp" / "state" / "regime_decision_state.json"


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.exists():
        # A submitted agent must boot from a clean checkout. ``online_adapt``
        # runs before ``regime_decision``, so no file means no history yet.
        # ``regime_decision`` atomically creates initial state later this tick.
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Persistent strategy state is unreadable; refusing online adaptation: {exc}") from exc


def _save_state(state: Dict[str, Any]) -> None:
    from condor.fsutil import atomic_write_json
    atomic_write_json(_state_path(), state, indent=2, sort_keys=True)


def _completed_history(state: Dict[str, Any], limit: int) -> list[Dict[str, Any]]:
    rows = list(state.get("hourly_history") or [])
    deduped: Dict[float, Dict[str, Any]] = {}
    for row in rows:
        key = float(row.get("decision_bar_timestamp") or 0.0)
        if key > 0:
            deduped[key] = row
    return [deduped[k] for k in sorted(deduped)][-limit:]


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    from routines.base import RoutineResult
    from condor.reports import ReportBuilder

    now = datetime.now(timezone.utc)
    state = _load_state()
    audit = list(state.get("audit") or [])
    reviews = [row for row in audit if row.get("kind") == "online_adaptation"]
    last_review = _parse(reviews[-1].get("at")) if reviews else None
    due = last_review is None or now - last_review >= timedelta(hours=config.review_hours)
    history = _completed_history(state, config.lookback_hours)
    receipts = [row for row in audit if row.get("kind") == "decision_receipt"]
    receipt = receipts[-1] if receipts else None
    guard = state.get("guard") or {}

    result: Dict[str, Any]
    if not config.enabled:
        result = {"status": "DISABLED"}
    elif not due:
        result = {"status": "NOT_DUE", "next_review_after": (last_review + timedelta(hours=config.review_hours)).isoformat()}
    elif guard.get("halted"):
        result = {"status": "HARD_HALT", "detail": guard.get("halt_reason")}
    elif len(history) < 2 or receipt is None:
        result = {"status": "INSUFFICIENT_HISTORY", "completed_bars": len(history)}
    else:
        first, last = history[0], history[-1]
        net_change = float(last.get("equity") or 0.0) - float(first.get("equity") or 0.0)
        active = [row for row in history if row.get("lp_on")]
        in_range = [row for row in active if row.get("in_range")]
        in_range_rate = len(in_range) / len(active) if active else None
        economics = receipt.get("economic_gate") or {}
        profile = str(receipt.get("profile") or "")
        existing = state.get("tuning") or {}

        if profile in {"paused", "laya_pause"}:
            result = {"status": "SAFETY_PAUSE", "detail": receipt.get("reason")}
        else:
            economic_bad = bool(
                economics.get("enabled")
                and float(economics.get("expected_fees_usd") or 0.0)
                    < config.fee_cost_multiple * float(economics.get("modeled_cost_usd") or 0.0)
            )
            pause = net_change <= -config.loss_pause_usd or economic_bad
            if pause:
                proposal = {
                    "hedge_ratio": 1.0,
                    "capital_fraction": 0.0,
                    "range_width_pct": 0.013,
                }
                rationale = (
                    f"online rollback: net equity {net_change:.2f}; economic gate "
                    f"{economics.get('expected_fees_usd', 0.0):.4f}/"
                    f"{economics.get('required_fees_usd', 0.0):.4f}"
                )
            else:
                width = 0.013 if in_range_rate is None or in_range_rate < 0.8 else float(
                    last.get("range_width_pct") or 0.013
                )
                width = min(RANGE_WIDTHS, key=lambda value: abs(value - width))
                proposal = {
                    "hedge_ratio": 1.0,
                    "capital_fraction": 1.0,
                    "range_width_pct": width,
                }
                rationale = (
                    f"online active: net equity {net_change:.2f}; "
                    f"in-range rate {0.0 if in_range_rate is None else in_range_rate:.2f}"
                )

            existing_expiry = _parse(existing.get("expires_at"))
            same = (
                existing_expiry is not None
                and existing_expiry > now
                and all(float(existing.get(key, -1.0)) == value for key, value in proposal.items())
            )
            record = {
                "kind": "online_adaptation",
                "at": now.isoformat(),
                "lookback_bars": len(history),
                "net_equity_change": round(net_change, 6),
                "in_range_rate": in_range_rate,
                "economic_gate": economics,
                "proposal": proposal,
                "rationale": rationale,
                "changed": not same,
            }
            audit.append(record)
            state["audit"] = audit[-200:]
            if not same:
                state["tuning"] = {
                    **proposal,
                    "rationale": rationale,
                    "at": now.isoformat(),
                    "expires_at": (now + PROPOSAL_TTL).isoformat(),
                    "source": "online_self_modification",
                }
            _save_state(state)
            result = {
                "status": "PROPOSED" if not same else "REAFFIRMED",
                "proposal": proposal,
                "rationale": rationale,
                "lookback_bars": len(history),
            }

    builder = ReportBuilder("Regime Switch LP — Online Adaptation")
    builder.source("routine", "online_adapt").tags(["regime_switch_lp", "online_adaptation", "risk"])
    builder.section("01 / RESULT", "Bounded policy review; hard controls remain immutable")
    builder.kpi("Status", result["status"])
    builder.markdown(json.dumps(result, indent=2))
    builder.section("02 / IMMUTABLE BOUNDARY", "What this routine cannot change")
    builder.markdown(
        "Pool/pair identity, code, tools, dollar stops, delta/basis/leverage caps, "
        "opening limits, economic gate, safety pauses, and halt clearing are immutable."
    )
    builder.manual_order()
    await builder.save()
    return RoutineResult(text=json.dumps(result, allow_nan=False))
