"""Agent activity reports — the companion push channel.

The TUI bridge only sees terminal commands. Everything else an agent does
(reading files, editing code, thinking) is invisible to it — unless the
agent says something. This module is that something.

Any agent (side-chat, subagent, script) drops a JSON heartbeat::

    ~/.muse/activity/<agent-id>.json
    {"agent": "<agent-id>", "label": "side-chat · lipi",
     "task": "Fixing dashboard drag reorder",
     "status": "editing DashboardPage.tsx",
     "todo": "_auto_fix-dashboard-dr_a1b2c3d4.md",
     "t0": 1759212345.0, "at": 1759212400.0}

The TUI polls this dir (~1s) and shows live agents in the activity line
and the sidebar agents panel, with elapsed time. Rules:

- Update ``at`` (and ``status``) as you work — at least every couple of
  minutes while active. The file's freshness is your liveness signal.
- If ``todo`` names one of your todo files, the TUI touches it: reporting
  doubles as your todo-list heartbeat (see auto_todo).
- Reports older than 2 minutes render dim (stale); older than 15 minutes
  are dropped from the UI. Stopping updates = "I'm done talking".
- Delete the file (or stop updating it) when the task is over.
- ``muse-cli report`` is the convenient writer; the JSON drop is the raw
  protocol for anything that can't shell out.
"""
from __future__ import annotations

import json
import os
import re
import time

from .paths import ACTIVITY_DIR, ensure_dirs

#: "at" newer than this renders as live.
LIVE_S = 120
#: Older than this is dropped from the UI entirely.
DROP_S = 15 * 60


def _safe_agent(agent_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", (agent_id or "").strip())
    return safe[:64] or "agent"


def report(agent_id: str, task: str = "", status: str = "",
           todo: str = "", label: str = "") -> str:
    """Write/refresh this agent's activity heartbeat. Returns the path."""
    ensure_dirs()
    path = os.path.join(ACTIVITY_DIR, _safe_agent(agent_id) + ".json")
    now = time.time()
    rec: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        pass
    if not isinstance(rec, dict):
        rec = {}
    rec["agent"] = agent_id
    if label:
        rec["label"] = label
    if task:
        rec["task"] = task
    if status:
        rec["status"] = status
    if todo:
        rec["todo"] = os.path.basename(todo)
    rec.setdefault("t0", now)
    rec["at"] = now
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return path


def clear(agent_id: str) -> None:
    """Agent is done talking — remove its report."""
    try:
        os.remove(os.path.join(ACTIVITY_DIR, _safe_agent(agent_id) + ".json"))
    except OSError:
        pass


def read_all(now: float | None = None) -> dict[str, dict]:
    """All current reports, annotated with live/stale/elapsed."""
    now = now if now is not None else time.time()
    out: dict[str, dict] = {}
    try:
        names = os.listdir(ACTIVITY_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(ACTIVITY_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        at = rec.get("at") or 0
        try:
            age = now - float(at)
        except (TypeError, ValueError):
            age = float("inf")
        if age > DROP_S:
            continue
        rec = dict(rec)
        rec["age"] = age
        rec["live"] = age <= LIVE_S
        t0 = rec.get("t0") or at or now
        try:
            rec["elapsed"] = max(0.0, now - float(t0))
        except (TypeError, ValueError):
            rec["elapsed"] = 0.0
        out[name[:-5]] = rec
    return out
