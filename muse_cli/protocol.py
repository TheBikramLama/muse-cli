"""Queue/result file protocol.

Two-way bridge:
  - Muse submits: write ~/.muse/queue/<uuid>.json, poll ~/.muse/results/<uuid>.json
  - Local user submits: via the TUI input box (same files, source="local")
  - Bridge executes, writes the result file, TUI shows it live.

Request::
    {"id": str, "task": str, "cmd": [str, ...], "cwd": str|None,
     "timeout": int|None, "source": "muse"|"local", "reveal": bool,
     "submitted_at": float,
     "steps": [{"name": str, "cmd": [str, ...], "cwd": str|None}] | None}

`task` is the human summary shown in the TUI ("Sync feature branch"),
`cmd` stays hidden unless revealed. `reveal: true` shows commands immediately.

Multi-step: instead of a single `cmd`, a request may carry `steps`. The
bridge runs them sequentially inside one task card, stops at the first
failing step, and reports per-step outcomes in the result's `steps` array.
Each step's `cwd` defaults to the request's `cwd`.

`needs_approval: true` parks the request in `~/.muse/approval/` instead of
running it — the TUI shows it as awaiting and the user approves (`a`) or
denies (`d`). Denied tasks get a `denied by user` result.
"""
from __future__ import annotations

import json
import os
import time
import uuid

from .paths import CANCEL_DIR, QUEUE_DIR, RESULTS_DIR, ensure_dirs


def new_id() -> str:
    return uuid.uuid4().hex


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def submit(task: str, cmd: list | None = None, cwd: str | None = None,
           timeout: int | None = None, source: str = "local",
           reveal: bool = False, steps: list | None = None) -> str:
    """Queue a request. Returns its id."""
    ensure_dirs()
    rid = new_id()
    req = {
        "id": rid,
        "task": task,
        "cmd": cmd or [],
        "cwd": cwd,
        "timeout": timeout,
        "source": source,
        "reveal": reveal,
        "submitted_at": time.time(),
    }
    if steps:
        req["steps"] = steps
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


def cancel(rid: str) -> None:
    """Request cancellation of a running (or queued) task."""
    ensure_dirs()
    open(os.path.join(CANCEL_DIR, rid), "w").close()
