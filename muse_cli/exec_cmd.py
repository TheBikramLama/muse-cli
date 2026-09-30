"""muse-cli exec: run one command through the bridge, synchronously.

Queues the command in ~/.muse/queue, waits for a running bridge (the TUI
or `muse-cli --daemon`) to pick it up, then prints the result's stdout /
stderr and exits with the command's exit code. Same guardrails as the
bridge itself: the executable must be on the settings allowlist, argv is
never a shell string, and the timeout is clamped to settings.max_timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import paths
from .bridge import CLAIM_SUFFIX
from .config import load_settings
from .paths import DEFAULT_SESSION, valid_session
from .protocol import new_id

POLL_S = 0.5
# How long to wait for a bridge instance to claim the queued request before
# concluding nobody is listening (the bridge renames the queue file to
# <id>.json.claimed.<pid> when it picks it up — see bridge.py).
NO_BRIDGE_S = 15

EXIT_USAGE = 2
EXIT_NO_BRIDGE = 3
EXIT_TIMEOUT = 124  # like GNU timeout


def _eprint(*args) -> None:
    print(*args, file=sys.stderr)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="muse-cli exec",
        description="Run a command via the muse-cli bridge and wait for "
                    "the result.")
    p.add_argument("--cwd", default=None,
                   help="working directory (default: current directory)")
    p.add_argument("--timeout", type=int, default=None, metavar="SEC",
                   help="max seconds to wait (default: settings default_timeout, "
                        "clamped to max_timeout)")
    p.add_argument("--session", default=DEFAULT_SESSION, metavar="NAME",
                   help="bridge session to route to (default: main)")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="command and arguments; use -- to separate from options")
    return p.parse_args(argv)


def _atomic_write(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _read_result(rid: str) -> dict | None:
    """Read the result file; None when missing or torn (retry next poll)."""
    try:
        with open(os.path.join(paths.RESULTS_DIR, rid + ".json")) as f:
            return json.load(f)
    except Exception:
        return None


def _take_result(rid: str) -> None:
    try:
        os.remove(os.path.join(paths.RESULTS_DIR, rid + ".json"))
    except OSError:
        pass


def _queue_path(rid: str) -> str:
    return os.path.join(paths.QUEUE_DIR, rid + ".json")


def _claimed(rid: str) -> bool:
    """True once a bridge instance has picked up the request."""
    try:
        names = os.listdir(paths.QUEUE_DIR)
    except OSError:
        return False
    prefix = rid + ".json" + CLAIM_SUFFIX + "."
    return any(n.startswith(prefix) for n in names)


def _drop_cancel(rid: str) -> None:
    try:
        os.makedirs(paths.CANCEL_DIR, exist_ok=True)
        open(os.path.join(paths.CANCEL_DIR, rid), "w").close()
    except OSError:
        pass


def exec_main(argv: list[str]) -> int:
    args = parse_args(argv)
    # nargs=REMAINDER captures a literal "--" as a positional, so strip it.
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        _eprint("muse-cli exec: no command given "
                "(usage: muse-cli exec [--cwd DIR] [--timeout SEC] "
                "[--session NAME] -- <cmd...>)")
        return EXIT_USAGE

    settings = load_settings()

    # Same check as runner.py: basename must be on the allowlist.
    exe = os.path.basename(cmd[0])
    if exe not in settings.get("allowlist", []):
        _eprint(f"muse-cli exec: executable '{exe}' is not in the allowlist "
                f"(edit ~/.muse/settings.json to add it)")
        return EXIT_USAGE

    session = args.session or DEFAULT_SESSION
    if not valid_session(session):
        _eprint(f"muse-cli exec: invalid session name '{session}'")
        return EXIT_USAGE

    timeout = args.timeout
    if timeout is None:
        timeout = settings.get("default_timeout", 120)
    timeout = max(1, min(timeout, settings.get("max_timeout", 1500)))

    rid = new_id()
    req = {
        "id": rid,
        "task": "exec: " + " ".join(cmd),
        "cmd": cmd,
        "cwd": args.cwd or os.getcwd(),
        "timeout": timeout,
        "source": "muse-cli-exec",
        "submitted_at": time.time(),
        "session": session,
    }
    try:
        os.makedirs(paths.QUEUE_DIR, exist_ok=True)
        _atomic_write(_queue_path(rid), req)
    except OSError as e:
        _eprint(f"muse-cli exec: could not write queue: {e}")
        return EXIT_USAGE

    start = time.time()
    claim_by = start + NO_BRIDGE_S
    give_up_at = start + timeout
    claimed = False
    while True:
        now = time.time()
        if not claimed and _claimed(rid):
            claimed = True
        res = _read_result(rid)
        if res is not None:
            _take_result(rid)
            sys.stdout.write(res.get("stdout") or "")
            sys.stderr.write(res.get("stderr") or "")
            sys.stdout.flush()
            sys.stderr.flush()
            code = res.get("exit")
            return code if isinstance(code, int) else 1
        if not claimed and now >= claim_by:
            _eprint("muse-cli exec: no bridge claimed the request within "
                    f"{NO_BRIDGE_S}s — start the TUI (./run.sh) or "
                    "`muse-cli --daemon`")
            return EXIT_NO_BRIDGE  # queue file left in place on purpose
        if now >= give_up_at:
            _drop_cancel(rid)
            _eprint(f"muse-cli exec: timed out after {timeout}s; wrote cancel "
                    f"for request {rid}")
            _eprint(f"the bridge may still finish it — check "
                    f"~/.muse/results/{rid}.json later")
            return EXIT_TIMEOUT
        time.sleep(POLL_S)
