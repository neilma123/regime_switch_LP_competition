"""Advisory parameter proposal, bounded by the frozen envelope; the supervisor re-validates before use."""
import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from routines.base import RoutineResult
from regime_switch_runtime.__main__ import load_config
from regime_switch_runtime.tuning import COMPETITION_ENVELOPE, tuning_path, write_proposal

CATEGORY = 'Regime Switch LP'


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid')
    hedge_ratio: float = Field(default=.8, description='One of 0.5, 0.8, 1.0 (share of LP SOL hedged)')
    capital_fraction: float = Field(default=.7, description='One of 0, 0.4, 0.7, 1.0 of the LP cap; 0 pauses the LP')
    range_width_pct: float = Field(default=.01, description='One of 0.007, 0.01, 0.013 (half-width)')
    rationale: str = Field(default='', description='Why, in one or two sentences, citing the diagnosis')


async def run(config: Config, context) -> RoutineResult:
    config_path = os.environ.get('REGIME_CONFIG_PATH')
    state_path = os.environ.get('REGIME_STATE_PATH')
    if not config_path or not state_path:
        return RoutineResult(text=json.dumps({'status': 'REFUSED', 'detail': 'no deployment configured'}))
    cfg = load_config(config_path)
    if not cfg.tuning_enabled:
        return RoutineResult(text=json.dumps({'status': 'REFUSED', 'detail': 'tuning_enabled is false in the frozen configuration'}))
    if not config.rationale.strip():
        return RoutineResult(text=json.dumps({'status': 'REFUSED', 'detail': 'a rationale is required'}))
    try:
        proposal = write_proposal(tuning_path(state_path), config.model_dump(), COMPETITION_ENVELOPE, int(time.time()))
    except ValueError as exc:
        return RoutineResult(text=json.dumps({'status': 'REJECTED', 'detail': str(exc),
                                              'envelope': COMPETITION_ENVELOPE.describe()}))
    return RoutineResult(text=json.dumps({'status': 'PROPOSED', 'proposal': proposal.to_dict(),
                                          'note': 'Advisory only. The supervisor applies it on its next tick if the '
                                                  'envelope, expiry and safety rules still allow it, and logs the outcome '
                                                  'under state.tuning. Dollar caps, stops and the halt are unchanged.'}))
