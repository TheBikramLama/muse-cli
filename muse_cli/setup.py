"""`muse-cli setup` and `muse-cli doctor`.

setup:   first-run onboarding. Creates ~/.muse, generates a CLI identity +
         pairing code, and prints exactly what to tell the Muse app so it can
         find this CLI. Non-interactive (safe when piped from curl).
doctor:  connection diagnostics. Reports whether the Muse link is active or
         inactive, whether the TUI is running, and whether the bridge dirs are
         healthy. Exit code 0 = all good, 1 = something needs attention.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import uuid

from . import __version__
from .paths import (ALL_DIRS, IDENTITY_PATH, MUSE_HOME, SETTINGS_PATH,
                    WATCHER_JSON, WATCHER_STALE_S, ensure_dirs)

_PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I/L/O/0/1


def _pairing_code() -> str:
    raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def load_identity() -> dict | None:
    try:
        with open(IDENTITY_PATH, encoding="utf-8") as f:
            ident = json.load(f)
        if isinstance(ident, dict) and ident.get("pairing_code"):
            return ident
    except (OSError, ValueError):
        pass
    return None


def _ok(msg: str) -> None:
    print(f"  \u2713 {msg}")


def _bad(msg: str) -> None:
    print(f"  \u2717 {msg}")


def _python_ok() -> tuple[bool, str]:
    vi = sys.version_info
    py = f"{vi.major}.{vi.minor}.{vi.micro}"
    return (vi >= (3, 10), py)


def cmd_setup() -> int:
    print(f"muse-cli setup  (v{__version__})")
    print("=" * 46)

    # 1. Python
    ok_py, py = _python_ok()
    if not ok_py:
        _bad(f"Python {py} found \u2014 muse-cli needs 3.10+")
        return 1
    _ok(f"Python {py}")

    # 2. Directories
    try:
        ensure_dirs()
    except OSError as e:
        _bad(f"could not create {MUSE_HOME}: {e}")
        return 1
    missing = [d for d in ALL_DIRS if not os.path.isdir(d)]
    if missing:
        _bad(f"missing directories: {', '.join(missing)}")
        return 1
    _ok(f"bridge directories at {MUSE_HOME}")

    # 3. Identity / pairing code (stable: never regenerate silently)
    ident = load_identity()
    if ident is None:
        ident = {
            "cli_id": uuid.uuid4().hex,
            "pairing_code": _pairing_code(),
            "created_at": int(time.time()),
            "version": __version__,
        }
        try:
            with open(IDENTITY_PATH, "w", encoding="utf-8") as f:
                json.dump(ident, f, indent=2)
        except OSError as e:
            _bad(f"could not write {IDENTITY_PATH}: {e}")
            return 1
        _ok("new CLI identity generated")
    else:
        _ok("existing CLI identity kept")

    # 4. Settings file exists (TUI bootstraps defaults; just ensure readable)
    if os.path.isfile(SETTINGS_PATH):
        _ok("settings.json present")
    else:
        _ok("settings.json will be created on first launch")

    code = ident["pairing_code"]
    print()
    print(f"  Your pairing code:  {code}")
    print("  (Keep it private \u2014 it identifies this machine's CLI.)")
    print()
    print("  Next steps:")
    print("    1. Start the CLI:   muse-cli")
    print("    2. In the Muse app, tell your assistant:")
    print()
    print(f'       "Connect to my muse-cli (pairing code {code})."')
    print('       "Protocol: https://github.com/TheBikramLama/muse-cli/blob/main/PROTOCOL.md"')
    print(f'       "Bridge directories: {MUSE_HOME}/"')
    print()
    print("  Your assistant will send a ping \u2014 you'll see it as a task card.")
    print("  The status line shows the link state: CLI \u2194 Muse \u25cf connected")
    print("  (or \u25cb not seen / \u26a0 silent while it sorts itself out).")
    print()
    print("  Run `muse-cli doctor` any time to check the connection.")
    return 0


def _watcher_state() -> tuple[str, str]:
    """Return (state, detail) for the Muse link.

    state: "active" | "stale" | "error" | "never".
    """
    try:
        with open(WATCHER_JSON, encoding="utf-8") as f:
            w = json.load(f)
    except (OSError, ValueError):
        return "never", "no heartbeat yet"
    if not isinstance(w, dict):
        return "never", "no heartbeat yet"
    if not w.get("ok"):
        return "error", str(w.get("error") or "watcher reported an error")
    at = w.get("at")
    if not isinstance(at, (int, float)):
        return "never", "no heartbeat yet"
    age = time.time() - at
    if age <= WATCHER_STALE_S:
        return "active", f"heartbeat {age:.0f}s ago"
    return "stale", f"last heartbeat {age / 60:.0f}m ago"


def _tui_running() -> bool:
    pid_path = os.path.join(MUSE_HOME, "muse-cli.pid")
    try:
        with open(pid_path, encoding="utf-8") as f:
            pid = int(f.read().strip().split()[0])
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmd_doctor() -> int:
    print(f"muse-cli doctor  (v{__version__})")
    print("=" * 46)
    problems = 0

    # Directories
    missing = [d for d in ALL_DIRS if not os.path.isdir(d)]
    if missing:
        _bad(f"missing: {', '.join(missing)}  (run `muse-cli setup`)")
        problems += 1
    else:
        _ok(f"bridge directories at {MUSE_HOME}")

    # Queue writable?
    q = os.path.join(MUSE_HOME, "queue")
    if os.path.isdir(q) and os.access(q, os.W_OK):
        _ok("queue writable")
    else:
        _bad("queue not writable")
        problems += 1

    # Identity
    ident = load_identity()
    if ident:
        _ok(f"CLI identity (pairing code {ident['pairing_code']})")
    else:
        _bad(f"no CLI identity  (run `muse-cli setup`)")
        problems += 1

    # TUI
    if _tui_running():
        _ok("TUI running")
    else:
        _bad("TUI not running  (start with: muse-cli)")
        problems += 1

    # Muse link
    state, detail = _watcher_state()
    if state == "active":
        print(f"  \u25cf Muse link: active ({detail})")
    elif state == "stale":
        _bad(f"Muse link: silent \u2014 {detail}")
        problems += 1
    elif state == "error":
        _bad(f"Muse link: error \u2014 {detail}")
        problems += 1
    else:
        print("  \u25cb Muse link: inactive (no heartbeat yet \u2014")
        print("    finish `muse-cli setup`, start the TUI, then ask your")
        print("    Muse assistant to ping this CLI)")

    print()
    if problems:
        print(f"  {problems} problem(s) found \u2014 see above.")
        return 1
    print("  All good.")
    return 0
