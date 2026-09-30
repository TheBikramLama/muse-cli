"""Agent activity reports — the companion push channel.

The TUI bridge only sees terminal commands. Everything else an agent does
(reading files, editing code, thinking) is invisible to it — unless the
agent says something. This module is that something.

Any agent (side-chat, subagent, script) drops a JSON heartbeat::

    ~/.muse/activity/<agent-id>.json
    {"agent": "<agent-id>", "label": "side-chat · lipi",
     "task": "Fixing dashboard drag reorder",
     "status": "editing DashboardPage.tsx",
     "state": "working", "reason": "",
     "todo": "_auto_fix-dashboard-dr_a1b2c3d4.md",
     "t0": 1759212345.0, "at": 1759212400.0}

The TUI polls this dir (~1s) and shows live agents in the activity line
and the sidebar agents panel, with elapsed time. Rules:

- Update ``at`` (and ``status``) as you work — at least every couple of
  minutes while active. The file's freshness is your liveness signal.
- ``state`` is one of: ``working`` (actively working), ``waiting``
  (blocked on the user — put why in ``reason``), ``stalled`` (stuck;
  the TUI also auto-detects this when a ``working`` agent goes quiet for
  5+ minutes), ``done`` (finished), ``failed`` (put why in ``reason``).
  Report it honestly as your situation changes.
- ``label`` should be your side-chat's display name (e.g. "lipi
  dashboard bugs"); the agent id stays the stable ``side-chat:<chat-id>``
  form so the TUI can track you across reports.
- If ``todo`` names one of your todo files, the TUI touches it: reporting
  doubles as your todo-list heartbeat (see auto_todo).
- Reports older than 2 minutes render dim; ``working`` agents quiet for
  5+ minutes show as stalled; older than 15 minutes are dropped from the
  UI. Stopping updates = "I'm done talking".
- ``--done`` (``mark_done``) marks you finished: you stay visible as done
  for 5 minutes, then vanish. Deleting the file (``clear``) signs you off
  immediately instead.
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
#: A "working" agent quieter than this is shown as stalled.
STALLED_S = 5 * 60
#: A "done" report stays visible for this long after finishing.
DONE_LINGER_S = 5 * 60
#: Older than this is dropped from the UI entirely.
DROP_S = 15 * 60

#: Valid explicit lifecycle states for report().
STATES = ("working", "waiting", "stalled", "done", "failed")


def _safe_agent(agent_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", (agent_id or "").strip())
    return safe[:64] or "agent"


def _write(path: str, rec: dict) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _load_rec(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def report(agent_id: str, task: str = "", status: str = "",
           todo: str = "", label: str = "", state: str = "",
           reason: str = "") -> str:
    """Write/refresh this agent's activity heartbeat. Returns the path.

    ``state`` is only overwritten when a valid non-empty value is given,
    so a routine heartbeat never resurrects a ``done`` agent.
    """
    ensure_dirs()
    path = os.path.join(ACTIVITY_DIR, _safe_agent(agent_id) + ".json")
    now = time.time()
    rec = _load_rec(path)
    rec["agent"] = agent_id
    if label:
        rec["label"] = label
    if task:
        rec["task"] = task
    if status:
        rec["status"] = status
    if todo:
        rec["todo"] = os.path.basename(todo)
    if state in STATES:
        rec["state"] = state
    if reason:
        rec["reason"] = reason
    rec.setdefault("t0", now)
    rec["at"] = now
    _write(path, rec)
    return path


def mark_done(agent_id: str, reason: str = "") -> str:
    """Mark the agent finished: stays visible as done, then vanishes.

    Refreshes ``at`` so the done linger counts from this moment; keeps
    task/label/t0. A later report() without ``state`` will not undo this.
    """
    ensure_dirs()
    path = os.path.join(ACTIVITY_DIR, _safe_agent(agent_id) + ".json")
    now = time.time()
    rec = _load_rec(path)
    rec["agent"] = agent_id
    rec["state"] = "done"
    rec["reason"] = reason
    rec.setdefault("t0", now)
    rec["at"] = now
    _write(path, rec)
    return path


def clear(agent_id: str) -> None:
    """Agent is done talking — remove its report immediately."""
    try:
        os.remove(os.path.join(ACTIVITY_DIR, _safe_agent(agent_id) + ".json"))
    except OSError:
        pass


def read_all(now: float | None = None) -> dict[str, dict]:
    """All current reports, annotated with live/stale/elapsed/state.

    Each record gains ``display_state``: one of ``working``, ``waiting``,
    ``stalled``, ``done``, ``failed``. Explicit states are honored as
    reported; a ``working`` (or stateless) agent quiet longer than
    STALLED_S is auto-flagged ``stalled``. ``done`` reports are dropped
    after DONE_LINGER_S; everything else drops after DROP_S.
    """
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
        rec = _load_rec(path)
        if not rec:
            continue
        at = rec.get("at") or 0
        try:
            age = now - float(at)
        except (TypeError, ValueError):
            age = float("inf")
        if age > DROP_S:
            continue
        state = rec.get("state") or ""
        if state not in STATES:
            state = ""
        if state == "done":
            if age > DONE_LINGER_S:
                continue
            display = "done"
        elif state == "failed":
            display = "failed"
        elif state == "waiting":
            display = "waiting"
        elif state == "stalled":
            display = "stalled"
        elif age > STALLED_S:
            display = "stalled"  # auto-detected: said working, went quiet
        else:
            display = "working"
        rec = dict(rec)
        rec["age"] = age
        rec["live"] = age <= LIVE_S
        rec["display_state"] = display
        t0 = rec.get("t0") or at or now
        try:
            rec["elapsed"] = max(0.0, now - float(t0))
        except (TypeError, ValueError):
            rec["elapsed"] = 0.0
        out[name[:-5]] = rec
    return out
