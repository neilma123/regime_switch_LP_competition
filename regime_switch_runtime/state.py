"""Atomic state and process lock; Linux is the Botcamp Docker target."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile


def save(path: Path, state):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.state-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(state,f,allow_nan=False,sort_keys=True);f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextmanager
def exclusive(path: Path):
    import fcntl
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.lock').open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: yield
        finally: fcntl.flock(f,fcntl.LOCK_UN)
