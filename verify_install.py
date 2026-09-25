"""Run from the target Condor root after installation. No credentials or writes."""
import asyncio
import importlib
import json
from pathlib import Path
import sys

root=Path.cwd().resolve()
sys.path.insert(0,str(root))
from routines.base import discover_routines_from_path
from condor.agents.agent import _load_agent_from_file
from regime_switch_runtime.settings import Settings

for name in ['engine','broker','operator','regime','capital_guard','profile','state']:
    module=importlib.import_module('regime_switch_runtime.'+name)
    assert Path(module.__file__).resolve().is_relative_to(root)
agent=root/'agents/regime_switch_lp'
assert _load_agent_from_file(agent/'AGENT.md','regime_switch_lp') is not None
routines=discover_routines_from_path(agent/'routines',agent_slug='regime_switch_lp',force_reload=True)
assert set(routines)=={'operator_tick'},list(routines)
Settings().validate()
print(json.dumps({'status':'INSTALL_AND_DISCOVERY_OK','routines':list(routines),'root':str(root)}))
if '--public-check' in sys.argv:
    routine=routines['operator_tick']
    result=asyncio.run(routine.run_fn(routine.config_class(),None))
    print(result.text)
