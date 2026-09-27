#!/usr/bin/env python3
"""
muse-runner.py -- lets Muse run dev commands on this Mac.

YOU start it, Muse sends commands through files in ~/.muse-runner/queue/.
Stop it any time with Ctrl+C. Nothing runs unless this script is running,
and every command it runs is printed below as it goes.

Usage:
    python3 ~/projects/lipi/muse-runner.py

Protocol (for Muse):
    - Write a request JSON to ~/.muse-runner/queue/<uuid>.json:
        {"cmd": ["git", "status"],
         "cwd": "/Users/bikram/projects/lipi/api.lipi.com.np",
         "timeout": 120}
      or {"ping": true} to check liveness.
    - Poll ~/.muse-runner/results/<uuid>.json until it appears, then read it:
        {"ok": true, "exit": 0, "stdout": "...", "stderr": "...",
         "truncated": false}
      or {"ok": false, "error": "..."} on rejection/failure.
    - Delete the result file when done.
"""

import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, ".muse-runner")
QUEUE = os.path.join(BASE, "queue")
RESULTS = os.path.join(BASE, "results")

# Commands Muse is allowed to run (by executable basename). Edit as you like.
ALLOWLIST = {
    "git", "docker", "npm", "npx", "node",
    "php", "composer", "curl",
    "ls", "pwd", "whoami", "env", "which",
    "cat", "head", "tail", "wc", "find", "mkdir", "echo",
}

# Commands may only run with a working directory inside these roots.
ALLOWED_ROOTS = [os.path.join(HOME, "projects", "lipi")]

DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1500
MAX_OUTPUT = 256 * 1024  # per stream
POLL_INTERVAL = 0.5
MAX_PARSE_ATTEMPTS = 20

_parse_attempts: dict = {}


def write_result(req_id, payload):
    tmp = os.path.join(RESULTS, req_id + ".json.tmp")
    final = os.path.join(RESULTS, req_id + ".json")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, final)


def fail(req_id, message):
    write_result(req_id, {"id": req_id, "ok": False, "error": message})


def valid_cwd(cwd):
    real = os.path.realpath(cwd)
    return any(real == r or real.startswith(r + os.sep) for r in ALLOWED_ROOTS)


def handle(req_id, req):
    if req.get("ping"):
        write_result(req_id, {"id": req_id, "ok": True, "pong": True})
        return

    cmd = req.get("cmd")
    if (not isinstance(cmd, list) or not cmd
            or not all(isinstance(a, str) for a in cmd)):
        fail(req_id, "'cmd' must be a non-empty list of strings")
        return

    exe = os.path.basename(cmd[0])
    if exe not in ALLOWLIST:
        fail(req_id, f"executable '{exe}' is not in the allowlist")
        return

    cwd = req.get("cwd") or ALLOWED_ROOTS[0]
    if not valid_cwd(cwd):
        fail(req_id, f"cwd '{cwd}' is outside allowed roots")
        return

    try:
        timeout = int(req.get("timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        fail(req_id, "'timeout' must be a number of seconds")
        return
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    print(f"$ (cd {cwd} && {' '.join(cmd)})", flush=True)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        fail(req_id, f"command timed out after {timeout}s")
        return
    except FileNotFoundError:
        fail(req_id, f"executable not found: {cmd[0]}")
        return
    except Exception as e:  # noqa: BLE001
        fail(req_id, f"runner error: {e}")
        return

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    truncated = False
    if len(stdout) > MAX_OUTPUT:
        stdout = stdout[:MAX_OUTPUT]
        truncated = True
    if len(stderr) > MAX_OUTPUT:
        stderr = stderr[:MAX_OUTPUT]
        truncated = True
    print(f"  -> exit {proc.returncode} "
          f"({len(stdout)} stdout, {len(stderr)} stderr bytes)", flush=True)
    write_result(req_id, {
        "id": req_id, "ok": True, "exit": proc.returncode,
        "stdout": stdout, "stderr": stderr, "truncated": truncated,
    })


def main():
    os.makedirs(QUEUE, exist_ok=True)
    os.makedirs(RESULTS, exist_ok=True)
    for name in os.listdir(RESULTS):  # drop stale results from last run
        if name.endswith(".json"):
            try:
                os.remove(os.path.join(RESULTS, name))
            except OSError:
                pass
    print("muse-runner started.")
    print(f"  queue:   {QUEUE}")
    print(f"  allowed: {', '.join(sorted(ALLOWLIST))}")
    print(f"  roots:   {', '.join(ALLOWED_ROOTS)}")
    print("Stop with Ctrl+C. Every command Muse runs is printed above.")
    sys.stdout.flush()
    try:
        while True:
            for name in sorted(os.listdir(QUEUE)):
                if not name.endswith(".json"):
                    continue
                req_id = name[:-5]
                path = os.path.join(QUEUE, name)
                try:
                    with open(path) as f:
                        req = json.load(f)
                except Exception:  # possibly a torn write; retry a few times
                    attempts = _parse_attempts.get(req_id, 0) + 1
                    _parse_attempts[req_id] = attempts
                    if attempts >= MAX_PARSE_ATTEMPTS:
                        fail(req_id, "could not read request file")
                        os.remove(path)
                        _parse_attempts.pop(req_id, None)
                    continue
                _parse_attempts.pop(req_id, None)
                os.remove(path)
                handle(req_id, req)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nmuse-runner stopped.")


if __name__ == "__main__":
    main()
