"""Append-only JSONL session history under ~/.muse/sessions/.

Every finished task is logged; the TUI restores recent history on launch so
restarting the app doesn't lose the story of what ran.
"""
from __future__ import annotations

import json
import os
import time

from .paths import SESSIONS_DIR, ensure_dirs


def new_session() -> str:
    ensure_dirs()
    return time.strftime("%Y%m%d-%H%M%S")


def log(sid: str, record: dict) -> None:
    ensure_dirs()
    record = dict(record)
    record.setdefault("at", time.time())
    try:
        with open(os.path.join(SESSIONS_DIR, sid + ".jsonl"), "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


def log_task(sid: str, res: dict) -> None:
    log(sid, {
        "type": "task",
        "id": res.get("id"),
        "task": res.get("task", ""),
        "source": res.get("source", "muse"),
        "cmd": res.get("cmd", []),
        "cwd": res.get("cwd"),
        "ok": res.get("ok"),
        "exit": res.get("exit"),
        "duration_s": res.get("duration_s", 0),
        "summary": res.get("summary") or res.get("error") or "",
        "skills": res.get("skills", []),
    })


def load_recent(limit: int = 50) -> list[dict]:
    """Newest-first history records across recent sessions, oldest-first out."""
    ensure_dirs()
    try:
        files = sorted(
            (f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl")),
            reverse=True,
        )
    except OSError:
        return []
    recs: list[dict] = []
    for name in files:
        try:
            with open(os.path.join(SESSIONS_DIR, name)) as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "task":
                recs.append(rec)
            if len(recs) >= limit:
                return list(reversed(recs))
    return list(reversed(recs))
