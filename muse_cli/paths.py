"""Filesystem layout. All data/persistence/sessions live under ~/.muse."""
from __future__ import annotations

import os
import re

MUSE_HOME = os.path.expanduser("~/.muse")

QUEUE_DIR = os.path.join(MUSE_HOME, "queue")
RESULTS_DIR = os.path.join(MUSE_HOME, "results")
SESSIONS_DIR = os.path.join(MUSE_HOME, "sessions")
SKILLS_DIR = os.path.join(MUSE_HOME, "skills")
SCRIPTS_DIR = os.path.join(MUSE_HOME, "scripts")
CANCEL_DIR = os.path.join(MUSE_HOME, "cancel")
APPROVAL_DIR = os.path.join(MUSE_HOME, "approval")
EXPORTS_DIR = os.path.join(MUSE_HOME, "exports")
SETTINGS_PATH = os.path.join(MUSE_HOME, "settings.json")
PAUSED_PATH = os.path.join(MUSE_HOME, "paused")  # file, not dir: bridge holds the queue while it exists
PID_PATH = os.path.join(MUSE_HOME, "muse-cli.pid")  # legacy guard path (session "main")
INPUT_HISTORY_PATH = os.path.join(MUSE_HOME, "input_history")

# Named instances: ./run.sh --session <name> runs parallel TUIs, one per
# session. Queue items, messages and replies carry a "session" tag so each
# instance only picks up its own work.
DEFAULT_SESSION = "main"
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def valid_session(name: str) -> bool:
    return bool(SESSION_RE.match(name or ""))


def pid_path(session: str = DEFAULT_SESSION) -> str:
    """Per-session instance lock. "main" keeps the historic path."""
    if session == DEFAULT_SESSION:
        return PID_PATH
    return os.path.join(MUSE_HOME, f"muse-cli.{session}.pid")

MESSAGES_DIR = os.path.join(MUSE_HOME, "messages")
REPLIES_DIR = os.path.join(MUSE_HOME, "replies")
TODOS_DIR = os.path.join(MUSE_HOME, "todos")
SEEN_PATH = os.path.join(MUSE_HOME, ".seen_replies")  # reply ids already shown (json list)
# Written by the Muse-side inbox watcher on every run: {"at", "ok", "state",
# "mid", "error"}. The TUI reads it for the watcher health segment and the
# "Muse is writing…" message state. A stale/missing file means the watcher
# is down — that absence is the signal.
WATCHER_JSON = os.path.join(MUSE_HOME, "watcher.json")
# Heartbeat older than this counts as a silent watcher (job runs every ~2m).
WATCHER_STALE_S = 300

ALL_DIRS = [QUEUE_DIR, RESULTS_DIR, SESSIONS_DIR, SKILLS_DIR, SCRIPTS_DIR,
            CANCEL_DIR, APPROVAL_DIR, EXPORTS_DIR,
            MESSAGES_DIR, REPLIES_DIR, TODOS_DIR]


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        os.makedirs(d, exist_ok=True)
