"""`muse-cli setup`, `muse-cli doctor`, `muse-cli pair`, `muse-cli unpair`.

setup:   first-run onboarding. Creates ~/.muse, generates a CLI identity,
         starts a pairing request and prints the copyable prompt for the
         Muse app. Non-interactive (safe when piped from curl): it only
         waits for the pairing receipt when stdin is a TTY.
pair:    interactive pairing — (re)prints the prompt and waits for the
         Muse app to complete the handshake.
unpair:  wipe pairing state so the next launch re-enters the flow.
doctor:  connection diagnostics. Reports pairing state, whether the Muse
         link is active, whether the TUI is running, and whether the
         bridge dirs are healthy. Exit 0 = all good, 1 = problems.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import __version__
from . import pairing
from .paths import (ALL_DIRS, IDENTITY_PATH, MUSE_HOME, SETTINGS_PATH,
                    WATCHER_JSON, WATCHER_STALE_S, ensure_dirs)


def load_identity() -> dict | None:
    try:
        with open(IDENTITY_PATH, encoding="utf-8") as f:
            ident = json.load(f)
        if isinstance(ident, dict) and ident.get("cli_id"):
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

    # 3. Identity (stable: never regenerate silently)
    try:
        ident = pairing.ensure_identity()
    except OSError as e:
        _bad(f"could not write {IDENTITY_PATH}: {e}")
        return 1
    _ok(f"CLI identity ({ident['cli_id'][:8]}…)")

    # 4. Settings file exists (TUI bootstraps defaults; just ensure readable)
    if os.path.isfile(SETTINGS_PATH):
        _ok("settings.json present")
    else:
        _ok("settings.json will be created on first launch")

    # 5. Pairing with the Muse app
    if pairing.is_paired():
        info = pairing.paired_info() or {}
        _ok(f"paired with {info.get('muse') or 'Muse app'}")
        print()
        print("  Already paired — run `muse-cli unpair` to reset and pair again.")
        print("  Run `muse-cli doctor` any time to check the connection.")
        return 0
    req = pairing.ensure_request(ident["cli_id"])
    _print_pairing_prompt(req)
    if sys.stdin.isatty():
        return _wait_for_pairing()
    print("  Run `muse-cli pair` to complete pairing.")
    return 0


def _print_pairing_prompt(req: dict) -> None:
    print()
    print(f"  Pairing code:  {req['code']}")
    print()
    print("  Paste this prompt into the Muse app to connect it:")
    print("  " + "-" * 42)
    for line in pairing.build_prompt(req).splitlines():
        print(line)
    print("  " + "-" * 42)


def _wait_for_pairing() -> int:
    print()
    print("  Waiting for the Muse app to complete pairing…")
    print("  (Ctrl+C to skip — run `muse-cli pair` when you're ready.)")
    try:
        while True:
            time.sleep(2)
            rec = pairing.check_receipt()
            if rec is not None:
                print()
                _ok(f"paired with {rec.get('muse') or 'Muse app'} ✓")
                print("  Start the TUI:  muse-cli")
                return 0
    except KeyboardInterrupt:
        print()
        print("  Skipped — run `muse-cli pair` when you're ready.")
        return 0


def cmd_pair() -> int:
    """(Re)run the interactive pairing flow."""
    ident = pairing.ensure_identity()
    if pairing.is_paired():
        info = pairing.paired_info() or {}
        print(f"Already paired with {info.get('muse') or 'Muse app'}.")
        print("Run `muse-cli unpair` first to pair again.")
        return 0
    req = pairing.ensure_request(ident["cli_id"])
    print(f"muse-cli pair  (v{__version__})")
    print("=" * 46)
    _print_pairing_prompt(req)
    return _wait_for_pairing()


def cmd_unpair() -> int:
    pairing.unpair()
    print("Unpaired — the next launch will ask to connect the Muse app again.")
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
        _ok(f"CLI identity ({ident['cli_id'][:8]}…)")
    else:
        _bad(f"no CLI identity  (run `muse-cli setup`)")
        problems += 1

    # Pairing with the Muse app
    if pairing.is_paired():
        info = pairing.paired_info() or {}
        _ok(f"paired with {info.get('muse') or 'Muse app'}")
    else:
        _bad("not paired with the Muse app  (run `muse-cli pair`)")
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
