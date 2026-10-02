"""Advisory tuning override for regime_switch_lp's regime_decision routine.

READ/WRITE-ONLY on state.tuning + state.audit: this routine never calls
create_lp_executor, create_order_executor, stop_executor, manage_clmm or any
other capital-moving tool. It proposes a (hedge_ratio, capital_fraction,
range_width_pct) override for regime_decision to apply on its own next tick,
subject to the frozen envelope, a 12h minimum change interval, an 18h expiry,
and regime_decision's own non-overridable safety pauses.
"""

CATEGORY = "Analysis"

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

HEDGE_RATIO_ENVELOPE = (0.5, 0.8, 1.0)
CAPITAL_FRACTION_ENVELOPE = (0.0, 1.0)
RANGE_WIDTH_PCT_ENVELOPE = (0.007, 0.01, 0.013)
SNAP_EPSILON = 1e-6
MIN_CHANGE_INTERVAL = timedelta(hours=12)
PROPOSAL_TTL = timedelta(hours=18)


class Config(BaseModel):
    """Propose a hedge_ratio/capital_fraction/range_width_pct override for regime_decision to apply next tick."""

    hedge_ratio: float = Field(
        default=1.0, description="Proposed hedge ratio: one of 0.5, 0.8, 1.0"
    )
    capital_fraction: float = Field(
        default=1.0,
        description="Proposed capital fraction: 0 or 1.0 (0 = pause; 1 = deploy the full LP allocation)",
    )
    range_width_pct: float = Field(
        default=0.01, description="Proposed LP range half-width pct: one of 0.007, 0.01, 0.013"
    )
    rationale: str = Field(
        default="",
        max_length=600,
        description="Required grounded rationale naming a diagnostic driver and numeric evidence",
    )


def _state_path():
    from condor.paths import local_agents_root

    return local_agents_root() / "regime_switch_lp" / "state" / "regime_decision_state.json"


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {"tuning": None, "audit": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Persistent strategy state is unreadable; refusing to propose tuning: {e}"
        ) from e
    raw.setdefault("tuning", None)
    raw.setdefault("audit", [])
    return raw


def _save_state(state: Dict[str, Any]) -> None:
    from condor.fsutil import atomic_write_json

    atomic_write_json(_state_path(), state, indent=2, sort_keys=True)


def _snap(value: float, allowed, epsilon: float = SNAP_EPSILON) -> Optional[float]:
    closest = min(allowed, key=lambda c: abs(c - value))
    if abs(closest - value) <= epsilon:
        return closest
    return None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    from routines.base import RoutineResult
    from condor.reports import ReportBuilder

    now = datetime.now(timezone.utc)
    rationale = config.rationale.strip()
    envelope = {
        "hedge_ratio": HEDGE_RATIO_ENVELOPE,
        "capital_fraction": CAPITAL_FRACTION_ENVELOPE,
        "range_width_pct": RANGE_WIDTH_PCT_ENVELOPE,
    }

    if not rationale:
        result = {"status": "REFUSED", "detail": "a rationale is required"}
    elif not re.search(r"[-+]?\d", rationale):
        result = {"status": "REFUSED", "detail": "rationale must cite numeric diagnostic evidence"}
    elif not any(
        driver in rationale.lower()
        for driver in (
            "net equity",
            "hedge",
            "fee",
            "out-of-range",
            "economic gate",
            "delta",
            "basis",
            "volatility",
            "funding",
            "whipsaw",
            "drawdown",
        )
    ):
        result = {
            "status": "REFUSED",
            "detail": "rationale must name a driver from diagnose/decision receipt",
        }
    else:
        snapped: Dict[str, float] = {}
        result = None
        for field, allowed in envelope.items():
            value = getattr(config, field)
            snap_value = _snap(value, allowed)
            if snap_value is None:
                result = {
                    "status": "REJECTED",
                    "detail": f"{field}={value} is outside the envelope",
                    "envelope": {k: list(v) for k, v in envelope.items()},
                }
                break
            snapped[field] = snap_value

        if result is None:
            state = _load_state()
            existing = state.get("tuning")
            is_reaffirmation = (
                existing is not None
                and existing.get("hedge_ratio") == snapped["hedge_ratio"]
                and existing.get("capital_fraction") == snapped["capital_fraction"]
                and existing.get("range_width_pct") == snapped["range_width_pct"]
            )

            blocked = False
            if not is_reaffirmation and existing is not None:
                last_at = _parse_iso(existing.get("at"))
                if last_at is not None and (now - last_at) < MIN_CHANGE_INTERVAL:
                    blocked = True

            if blocked:
                result = {
                    "status": "REJECTED",
                    "detail": "inside the minimum 12h change interval",
                }
            else:
                proposal = {
                    "hedge_ratio": snapped["hedge_ratio"],
                    "capital_fraction": snapped["capital_fraction"],
                    "range_width_pct": snapped["range_width_pct"],
                    "rationale": rationale,
                    "at": now.isoformat(),
                    "expires_at": (now + PROPOSAL_TTL).isoformat(),
                    "source": "agent",
                }
                state["tuning"] = proposal
                state.setdefault("audit", []).append(
                    {"kind": "tuning_proposed", "at": now.isoformat(), "tuning": proposal}
                )
                _save_state(state)
                result = {
                    "status": "PROPOSED",
                    "proposal": proposal,
                    "note": (
                        "Advisory only. regime_decision applies it on its next tick only if "
                        "the envelope, expiry and safety rules still allow it."
                    ),
                }

    builder = ReportBuilder("Regime Switch LP — Tuning Proposal")
    builder.source("routine", "propose_tuning").tags(["regime_switch_lp", "tuning", "risk"])
    builder.section("01 / PROPOSAL", "Advisory hedge/capital/range override for regime_decision")
    builder.kpi("Status", result["status"])
    builder.kpi("Requested hedge_ratio", str(config.hedge_ratio))
    builder.kpi("Requested capital_fraction", str(config.capital_fraction))
    builder.kpi("Requested range_width_pct", str(config.range_width_pct))
    if "proposal" in result:
        p = result["proposal"]
        builder.kpi("Applied hedge_ratio", str(p["hedge_ratio"]))
        builder.kpi("Applied capital_fraction", str(p["capital_fraction"]))
        builder.kpi("Applied range_width_pct", str(p["range_width_pct"]))
        builder.kpi("Expires", p["expires_at"])
    detail = result.get("detail") or result.get("note") or ""
    builder.markdown(f"**Rationale:** {rationale or '(none — refused)'}\n\n**Detail:** {detail}")
    builder.section("02 / ENVELOPE", "Frozen values this routine will ever accept")
    builder.table(
        [{"field": k, "allowed_values": ", ".join(str(x) for x in v)} for k, v in envelope.items()],
        ["field", "allowed_values"],
    )
    builder.manual_order()
    await builder.save()

    return RoutineResult(text=json.dumps(result, allow_nan=False))
