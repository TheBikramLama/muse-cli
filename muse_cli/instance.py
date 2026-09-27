"""Single-instance guard and launchd installation for muse-cli.

Only one bridge (TUI or --daemon) may run at a time: two instances racing
the queue could execute the same request twice.
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess

from .paths import MUSE_HOME, PID_PATH, ensure_dirs

LABEL = "com.muse.cli"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours
    except OSError:
        return False
    return True


def claim(mode: str) -> None:
    """Claim the single instance slot. Exits if another live instance holds it."""
    ensure_dirs()
    if os.path.exists(PID_PATH):
        try:
            with open(PID_PATH) as f:
                info = json.load(f)
            pid = int(info.get("pid", 0))
            other_mode = info.get("mode", "?")
        except (OSError, ValueError):
            pid, other_mode = 0, "?"
        if pid and pid != os.getpid() and _pid_alive(pid):
            print(f"muse-cli is already running as {other_mode} (pid {pid}).")
            if other_mode == "daemon":
                print("Unload it first: "
                      "launchctl unload -w ~/Library/LaunchAgents/com.muse.cli.plist")
            raise SystemExit(1)
    try:
        with open(PID_PATH, "w") as f:
            json.dump({"pid": os.getpid(), "mode": mode}, f)
    except OSError:
        print("warning: could not write the instance lock")
    atexit.register(release)


def release() -> None:
    """Drop the lock, but only if we still hold it."""
    try:
        with open(PID_PATH) as f:
            info = json.load(f)
        if int(info.get("pid", 0)) == os.getpid():
            os.remove(PID_PATH)
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
