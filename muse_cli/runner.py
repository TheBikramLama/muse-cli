"""Executes one queued request with guardrails.

- Executable allowlist (by basename)
- Working directory must sit under an allowed root (configurable, default ~)
- Commands are argv lists — never shell strings
- Timeouts, per-stream output caps
- Streams stdout lines to on_chunk for the TUI live view
"""
from __future__ import annotations

import os
import subprocess
import threading
import time


def _fail(rid: str, error: str, started: float | None = None,
          req: dict | None = None) -> dict:
    now = time.time()
    return {
        "id": rid,
        "ok": False,
        "error": error,
        "exit": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "summary": error,
        "started_at": started or now,
        "ended_at": now,
        "duration_s": round(now - (started or now), 1),
        "task": (req or {}).get("task", ""),
        "source": (req or {}).get("source", "muse"),
        "cmd": (req or {}).get("cmd", []),
        "cwd": (req or {}).get("cwd"),
    }


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def check_cwd(cwd: str | None, allowed_roots: list) -> tuple[str, str | None]:
    """Returns (resolved_cwd, error). Empty allowed_roots = anywhere."""
    resolved = _real(cwd) if cwd else os.path.expanduser("~")
    if not allowed_roots:
        return resolved, None
    roots = [_real(r) for r in allowed_roots]
    if any(resolved == r or resolved.startswith(r + os.sep) for r in roots):
        return resolved, None
    return resolved, f"cwd '{cwd}' is outside allowed roots {allowed_roots} (see ~/.muse/settings.json)"


def summarize(cmd: list, exit_code: int | None, stdout: str, stderr: str) -> str:
    status = "ok" if exit_code == 0 else f"exit {exit_code}"
    lines = [l for l in stdout.splitlines() if l.strip()]
    if lines:
        tail = lines[-1][:120]
    elif stderr.strip():
        tail = stderr.strip().splitlines()[-1][:120]
    else:
        tail = "no output"
    return f"{status} · {len(lines)} line(s) · {tail}"


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) > limit:
        return text[:limit], True
    return text, False


def run_request(req: dict, settings: dict, on_chunk=None, on_proc=None) -> dict:
    rid = req.get("id", "?")
    started = time.time()

    cmd = req.get("cmd")
    if (not isinstance(cmd, list) or not cmd
            or not all(isinstance(a, str) for a in cmd)):
        return _fail(rid, "'cmd' must be a non-empty list of strings", started, req)

    exe = os.path.basename(cmd[0])
    if exe not in settings.get("allowlist", []):
        return _fail(rid, f"executable '{exe}' is not in the allowlist "
                          f"(edit ~/.muse/settings.json to add it)", started, req)

    cwd, err = check_cwd(req.get("cwd"), settings.get("allowed_roots", ["~"]))
    if err:
        return _fail(rid, err, started, req)

    try:
        timeout = int(req.get("timeout") or settings.get("default_timeout", 120))
    except (TypeError, ValueError):
        return _fail(rid, "'timeout' must be a number of seconds", started, req)
    timeout = max(1, min(timeout, settings.get("max_timeout", 1500)))
    max_out = settings.get("max_output_bytes", 256 * 1024)

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for credentials

    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, errors="replace", bufsize=1,
        )
    except FileNotFoundError:
        return _fail(rid, f"executable not found: {cmd[0]}", started, req)
    except Exception as e:
        return _fail(rid, f"runner error: {e}", started, req)

    if on_proc:
        try:
            on_proc(proc)
        except Exception:
            pass

    out_lines: list[str] = []
    err_lines: list[str] = []

    def _reader(stream, buf, forward: bool):
        try:
            for line in stream:
                buf.append(line)
                if forward and on_chunk:
                    try:
                        on_chunk(line)
                    except Exception:
                        pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    # stdout streams live to the TUI; stderr is captured for the final result.
    t_out = threading.Thread(target=_reader, args=(proc.stdout, out_lines, True), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, err_lines, False), daemon=True)
    t_out.start()
    t_err.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        t_out.join(5)
        t_err.join(5)
        return _fail(rid, f"command timed out after {timeout}s", started, req)
    t_out.join()
    t_err.join()

    ended = time.time()
    stdout, t1 = _cap("".join(out_lines), max_out)
    stderr, t2 = _cap("".join(err_lines), max_out)
    return {
        "id": rid,
        "ok": True,
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": t1 or t2,
        "summary": summarize(cmd, proc.returncode, stdout, stderr),
        "started_at": started,
        "ended_at": ended,
        "duration_s": round(ended - started, 1),
        "task": req.get("task", ""),
        "source": req.get("source", "muse"),
        "cmd": cmd,
        "cwd": cwd,
    }


def _public_steps(ran: list[dict]) -> list[dict]:
    """Per-step outcome summaries safe to ship in the aggregate result."""
    return [{
        "name": r.get("task", ""),
        "ok": r["ok"],
        "exit": r.get("exit"),
        "duration_s": r.get("duration_s", 0),
        "summary": r.get("summary") or r.get("error") or "",
    } for r in ran]


def run_steps(req: dict, settings: dict, on_chunk=None, on_proc=None,
              on_step=None, is_cancelled=None) -> dict:
    """Run a request's steps sequentially; stops at the first failing step.

    A request without ``steps`` is a plain single-command request and is
    handled exactly like before (identical result shape). With ``steps``,
    each entry is ``{"name": str, "cmd": [str, ...], "cwd": str|None}``
    and the result aggregates the per-step outcomes.
    """
    started = time.time()
    rid = req.get("id", "?")
    raw = req.get("steps")
    if not raw:
        return run_request(req, settings, on_chunk=on_chunk, on_proc=on_proc)
    if not isinstance(raw, list):
        return _fail(rid, "'steps' must be a list of {name, cmd, cwd}", started, req)

    steps = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict):
            return _fail(rid, f"step {i} must be an object", started, req)
        cmd = s.get("cmd")
        if (not isinstance(cmd, list) or not cmd
                or not all(isinstance(a, str) for a in cmd)):
            return _fail(rid, f"step {i}: 'cmd' must be a non-empty list of strings",
                         started, req)
        steps.append({"name": s.get("name") or f"step {i}",
                      "cmd": cmd, "cwd": s.get("cwd", req.get("cwd"))})
    n = len(steps)
    if n == 0:
        return _fail(rid, "'steps' must not be empty", started, req)

    ran: list[dict] = []
    for i, step in enumerate(steps, 1):
        if is_cancelled is not None and is_cancelled():
            agg = _fail(rid, f"cancelled at step {i}/{n} ({step['name']})",
                        started, req)
            agg["steps"] = _public_steps(ran)
            return agg
        if on_step is not None:
            try:
                on_step(i, n, step["name"])
            except Exception:
                pass
        step_req = dict(req)
        step_req["task"] = step["name"]
        step_req["cmd"] = step["cmd"]
        step_req["cwd"] = step["cwd"]
        res = run_request(step_req, settings, on_chunk=on_chunk, on_proc=on_proc)
        ran.append(res)
        if not res["ok"] or (res.get("exit") or 0) != 0:
            break

    failed = next((r for r in ran
                   if not r["ok"] or (r.get("exit") or 0) != 0), None)
    ok = failed is None
    max_out = settings.get("max_output_bytes", 256 * 1024)
    stdout, t1 = _cap("\n".join(r["stdout"] for r in ran), max_out)
    stderr, t2 = _cap("\n".join(r["stderr"] for r in ran), max_out)
    if ok:
        summary = f"ok \u00b7 {n}/{n} steps"
    else:
        idx = ran.index(failed) + 1
        tail = (failed.get("summary") or failed.get("error") or "")[:120]
        summary = f"failed at step {idx}/{n} ({failed.get('task')}): {tail}"
    ended = time.time()
    return {
        "id": rid,
        "ok": ok,
        "exit": 0 if ok else failed.get("exit"),
        "stdout": stdout,
        "stderr": stderr,
        "truncated": t1 or t2,
        "summary": summary,
        "started_at": started,
        "ended_at": ended,
        "duration_s": round(ended - started, 1),
        "task": req.get("task", ""),
        "source": req.get("source", "muse"),
        "cmd": req.get("cmd", []),
        "cwd": req.get("cwd"),
        "steps": _public_steps(ran),
    }
