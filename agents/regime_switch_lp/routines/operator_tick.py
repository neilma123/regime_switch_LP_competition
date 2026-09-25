"""Run the frozen, reconciled Regime Switch LP policy. No LLM risk overrides."""
import json
from pydantic import BaseModel, ConfigDict
from routines.base import RoutineResult
from regime_switch_runtime.__main__ import run_once

CATEGORY = 'Regime Switch LP'

class Config(BaseModel):
    model_config = ConfigDict(extra='forbid')

async def run(config: Config, context) -> RoutineResult:
    result = await run_once()
    return RoutineResult(text=json.dumps(result,allow_nan=False))
