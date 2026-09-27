"""Per-session instance guard and launchd installation for muse-cli.

One bridge (TUI or --daemon) may run per session: two instances racing the
same session's queue could execute the same request twice. Different
sessions (./run.sh --session <name>) run side by side freely.
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess

from .paths import (DEFAULT_SESSION, MUSE_HOME, pid_path,
                    ensure_dirs, valid_session)

LABEL = "com.muse.cli"


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours
    except OSError:
        return False
    return True


def _read_lock(path: str) -> tuple[int, str]:
    try:
        with open(path) as f:
            info = json.load(f)
        return int(info.get("pid", 0)), info.get("mode", "?")
    except (OSError, ValueError):
        return 0, "?"


def claim(mode: str, session: str = DEFAULT_SESSION) -> None:
    """Claim the instance slot for a session. Exits if it's already held."""
    if not valid_session(session):
        print(f"bad session name: {session!r} "
              "(letters, digits, _ and - only)")
        raise SystemExit(1)
    ensure_dirs()
    lock = pid_path(session)
    # For session "main" this is the historic path, so an old-version
    # instance still holding it is seen here too.
    pid, other_mode = _read_lock(lock)
    if pid and pid != os.getpid() and pid_alive(pid):
        print(f"muse-cli is already running session {session!r} "
              f"as {other_mode} (pid {pid}).")
        if other_mode == "daemon":
            print("Unload it first: "
                  "launchctl unload -w ~/Library/LaunchAgents/com.muse.cli.plist")
        raise SystemExit(1)
    try:
        with open(lock, "w") as f:
            json.dump({"pid": os.getpid(), "mode": mode,
                       "session": session}, f)
    except OSError:
        print("warning: could not write the instance lock")
    atexit.register(release, lock)


def release(lock: str | None = None) -> None:
    """Drop the lock, but only if we still hold it."""
    path = lock or pid_path()
    try:
        with open(path) as f:
            info = json.load(f)
        if int(info.get("pid", 0)) == os.getpid():
            os.remove(path)
    except (OSError, ValueError):
        pass


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plist_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def install_launchd() -> None:
    root = _repo_root()
    plist = _plist_path()
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    log = os.path.join(MUSE_HOME, "daemon.log")
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{root}/run.sh</string>
    <string>--daemon</string>
  </array>
  <key>WorkingDirectory</key><string>{root}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""
    with open(plist, "w") as f:
        f.write(content)
    try:
        subprocess.run(["launchctl", "load", "-w", plist],
                       check=True, capture_output=True, timeout=15)
        print(f"installed and loaded: {plist}")
    except (OSError, subprocess.SubprocessError):
        print(f"wrote {plist} but launchctl is unavailable; "
              f"load it by hand: launchctl load -w {plist}")
    print("The daemon processes the queue in the background.")
    print("While it runs, the TUI will refuse to start (single instance) —")
    print("unload it first to use the TUI.")


def uninstall_launchd() -> None:
    plist = _plist_path()
    try:
        subprocess.run(["launchctl", "unload", "-w", plist],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        os.remove(plist)
        print(f"removed {plist}")
    except OSError:
        print(f"nothing installed at {plist}")
