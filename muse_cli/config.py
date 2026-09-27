"""~/.muse/settings.json management. The app reads and writes this live."""
from __future__ import annotations

import json
import os

from .paths import SETTINGS_PATH, ensure_dirs

DEFAULTS: dict = {
    # Executables Muse / the TUI are allowed to run (by basename).
    "allowlist": [
        "git", "docker", "npm", "npx", "node",
        "php", "composer", "curl",
        "python3", "pip", "pip3", "brew", "make",
        "ls", "pwd", "whoami", "env", "which",
        "cat", "head", "tail", "wc", "find", "mkdir", "echo",
        "rsync", "tar", "ssh", "open",
    ],
    # Working directories commands may run in. "~" = anywhere under home.
    # [] (empty list) = anywhere on the machine.
    "allowed_roots": ["~"],
    "default_timeout": 120,
    "max_timeout": 1500,
    "max_output_bytes": 256 * 1024,
    # When true, requests flagged needs_approval skip the approval park and
    # run immediately. Default off — approve/deny stays a human decision.
    "auto_approve": False,
    "tui": {
        "poll_interval": 0.5,
        "history_limit": 50,
        "notify_on_done": False,  # macOS notification when any task finishes
    },
}


def load_settings() -> dict:
    ensure_dirs()
    settings = dict(DEFAULTS)
    settings["tui"] = dict(DEFAULTS["tui"])
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH) as f:
                user = json.load(f)
            tui = settings["tui"]
            tui.update(user.get("tui", {}))
            settings.update(user)
            settings["tui"] = tui
        except Exception:
            pass  # corrupt settings -> fall back to defaults
    else:
        # First run: write the defaults so the file exists and is discoverable.
        try:
            save_settings(settings)
        except Exception:
            pass
    return settings


def save_settings(settings: dict) -> None:
    ensure_dirs()
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, indent=2)
    os.replace(tmp, SETTINGS_PATH)
