"""Install into a current Condor checkout, preserving overwritten files."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from datetime import datetime,timezone


def install(target):
    source=Path(__file__).resolve().parent;target=target.resolve()
    if not (target/'condor').is_dir() or not (target/'pyproject.toml').is_file():
        raise ValueError('Target must be a Condor checkout root')
    manifest=json.loads((source/'MANIFEST.json').read_text())['files']
    for name,expected in manifest.items():
        p=(source/name).resolve()
        if not p.is_relative_to(source) or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:
            raise ValueError('Release manifest verification failed')
    routine_dir=target/'agents/regime_switch_lp/routines'
    shipped={Path(n).name for n in manifest if n.startswith('agents/regime_switch_lp/routines/')}
    unexpected=[p.name for p in routine_dir.glob('*.py') if p.name not in shipped]
    if unexpected:
        raise ValueError('Legacy routines present; install in a clean checkout: '+', '.join(unexpected))
    backup=target/'submission_backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    copied=0
    for name in manifest:
        if not name.startswith(('regime_switch_runtime/','agents/regime_switch_lp/')): continue
        dst=target/name
        if dst.exists() and dst.read_bytes()!=(source/name).read_bytes():
            old=backup/name;old.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dst,old)
        dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,dst);copied+=1
    print(json.dumps({'installed_files':copied,'target':str(target),'backup':str(backup)}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--condor-root',type=Path,required=True)
    install(p.parse_args().condor_root)
