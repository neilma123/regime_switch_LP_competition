"""Run public diagnostics, a reconciled tick, or the deterministic supervisor."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from .broker import Broker
from .engine import tick
from .settings import Settings


def load_config(path):
    cfg=Settings(**json.loads(Path(path).read_text()))
    cfg.validate()
    return cfg


async def run_once(config_path=None,state_path=None):
    config_path=config_path or os.environ.get('REGIME_CONFIG_PATH')
    async with Broker() as broker:
        if not config_path:
            return await broker.public_check()
        cfg=load_config(config_path)
        state_path=state_path or os.environ.get('REGIME_STATE_PATH')
        if not state_path:
            raise ValueError('Set REGIME_STATE_PATH to a persistent file dedicated to this race and mode')
        return await tick(cfg,broker,Path(state_path))


async def main(args):
    if args.public_check:
        async with Broker() as broker:
            print(json.dumps(await broker.public_check(),allow_nan=False))
        return
    if not args.config and not os.environ.get('REGIME_CONFIG_PATH'):
        raise ValueError('Use --public-check or supply a frozen account configuration')
    while True:
        try:
            result=await run_once(args.config,args.state)
            print(json.dumps(result,allow_nan=False),flush=True)
        except Exception as exc:
            # Do not print credentials, HTTP request bodies or account material.
            print(json.dumps({'status':'UNVERIFIED_ERROR','error_type':type(exc).__name__}),flush=True)
            if not args.loop: raise SystemExit(1)
        if not args.loop: return
        await asyncio.sleep(15)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--public-check',action='store_true')
    p.add_argument('--config',type=Path)
    p.add_argument('--state',type=Path)
    p.add_argument('--loop',action='store_true')
    asyncio.run(main(p.parse_args()))
