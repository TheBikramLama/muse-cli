"""Queue/result file protocol.

Two-way bridge:
  - Muse submits: write ~/.muse/queue/<uuid>.json, poll ~/.muse/results/<uuid>.json
  - Local user submits: via the TUI input box (same files, source="local")
  - Bridge executes, writes the result file, TUI shows it live.

Request::
    {"id": str, "task": str, "cmd": [str, ...], "cwd": str|None,
     "timeout": int|None, "source": "muse"|"local", "reveal": bool,
     "submitted_at": float}

`task` is the human summary shown in the TUI ("Sync feature branch"),
`cmd` stays hidden unless revealed. `reveal: true` shows commands immediately.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from .paths import QUEUE_DIR, RESULTS_DIR, ensure_dirs


def new_id() -> str:
    return uuid.uuid4().hex


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def submit(task: str, cmd: list, cwd: str | None = None,
           timeout: int | None = None, source: str = "local",
           reveal: bool = False) -> str:
    """Queue a request. Returns its id."""
    ensure_dirs()
    rid = new_id()
    req = {
        "id": rid,
        "task": task,
        "cmd": cmd,
        "cwd": cwd,
        "timeout": timeout,
        "source": source,
        "reveal": reveal,
        "submitted_at": time.time(),
    }
    _atomic_write(os.path.join(QUEUE_DIR, rid + ".json"), req)
    return rid


def write_result(res: dict) -> None:
    ensure_dirs()
    _atomic_write(os.path.join(RESULTS_DIR, res["id"] + ".json"), res)


def read_result(rid: str) -> dict | None:
    """Peek at a result without removing it (torn writes -> None, retry)."""
    path = os.path.join(RESULTS_DIR, rid + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def take_result(rid: str) -> dict | None:
    res = read_result(rid)
    if res is not None:
        try:
            os.remove(os.path.join(RESULTS_DIR, rid + ".json"))
        except OSError:
            pass
    return res
