"""Filesystem layout. All data/persistence/sessions live under ~/.muse."""
from __future__ import annotations

import os

MUSE_HOME = os.path.expanduser("~/.muse")

QUEUE_DIR = os.path.join(MUSE_HOME, "queue")
RESULTS_DIR = os.path.join(MUSE_HOME, "results")
SESSIONS_DIR = os.path.join(MUSE_HOME, "sessions")
SKILLS_DIR = os.path.join(MUSE_HOME, "skills")
SCRIPTS_DIR = os.path.join(MUSE_HOME, "scripts")
SETTINGS_PATH = os.path.join(MUSE_HOME, "settings.json")

ALL_DIRS = [QUEUE_DIR, RESULTS_DIR, SESSIONS_DIR, SKILLS_DIR, SCRIPTS_DIR]


def ensure_dirs() -> None:
    for d in ALL_DIRS:
        os.makedirs(d, exist_ok=True)
