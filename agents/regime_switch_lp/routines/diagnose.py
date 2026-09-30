"""Read-only diagnosis packet: realised hourly history, the rule's view, the tuning envelope."""
import json
import os
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from routines.base import RoutineResult
from regime_switch_runtime.__main__ import load_config
from regime_switch_runtime.tuning import COMPETITION_ENVELOPE, diagnosis_packet, read_proposal, tuning_path

CATEGORY = 'Regime Switch LP'


class Config(BaseModel):
    model_config = ConfigDict(extra='forbid')
    lookback_hours: int = Field(default=24, ge=1, le=72, description='Hours of persisted history to attribute')


async def run(config: Config, context) -> RoutineResult:
    config_path = os.environ.get('REGIME_CONFIG_PATH')
    state_path = os.environ.get('REGIME_STATE_PATH')
    if not config_path or not state_path or not Path(state_path).exists():
        return RoutineResult(text=json.dumps({'status': 'NO_DEPLOYMENT',
                                              'detail': 'REGIME_CONFIG_PATH / REGIME_STATE_PATH unset or no state yet; nothing to diagnose'}))
    cfg = load_config(config_path)
    state = json.loads(Path(state_path).read_text())
    proposal = read_proposal(tuning_path(state_path), COMPETITION_ENVELOPE)
    packet = diagnosis_packet(state, cfg, int(time.time()), COMPETITION_ENVELOPE, config.lookback_hours, proposal)
    packet['tuning_enabled'] = cfg.tuning_enabled
    packet['status'] = state.get('status')
    return RoutineResult(text=json.dumps(packet, allow_nan=False))
