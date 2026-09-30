"""Bridge: watches ~/.muse/queue, runs commands, writes ~/.muse/results.

Used two ways:
  - Headless: `python -m muse_cli --daemon` (plain stdout logging, like the
    old muse-runner.py).
  - Embedded: the TUI starts a Bridge in a background thread and wires
    on_start / on_chunk / on_result callbacks to live widgets.

Cancellation: drop an empty file named <task-id> into ~/.muse/cancel/
(the TUI does this when you press `x`). The bridge kills the process and
reports the task as cancelled.

Settings are reloaded every loop so editing ~/.muse/settings.json applies live.
"""
from __future__ import annotations

import json
import os
import threading
import time

from .config import load_settings
from . import auto_todo
from .instance import pid_alive
from .paths import (APPROVAL_DIR, CANCEL_DIR, DEFAULT_SESSION, PAUSED_PATH,
                    QUEUE_DIR, ensure_dirs)
from .protocol import write_result
from .runner import run_request, run_steps

MAX_PARSE_ATTEMPTS = 20
CLAIM_SUFFIX = ".claimed"  # <rid>.json.claimed.<pid> while an instance owns it


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _short_cmd(cmd) -> str:
    """A human step label from a raw command list (no more 'step 3')."""
    if isinstance(cmd, (list, tuple)):
        text = " ".join(str(c) for c in cmd).strip()
    else:
        text = str(cmd or "").strip()
    return text[:48] + ("…" if len(text) > 48 else "")


def _cancelled_result(req: dict) -> dict:
    now = time.time()
    return {
        "id": req.get("id", "?"),
        "ok": False,
        "error": "cancelled",
        "exit": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "summary": "cancelled",
        "started_at": now,
        "ended_at": now,
        "duration_s": 0,
        "task": req.get("task", ""),
        "source": req.get("source", "muse"),
        "cmd": req.get("cmd", []),
        "cwd": req.get("cwd"),
    }


def _call(cb, *args) -> None:
    if cb:
        try:
            cb(*args)
        except Exception:
            pass


class Bridge:
    def __init__(self, settings: dict, on_start=None, on_chunk=None, on_result=None,
                 on_step=None, on_approval=None,
                 session: str = DEFAULT_SESSION):
        self.settings = settings
        self.session = session or DEFAULT_SESSION
        self.on_start = on_start
        self.on_chunk = on_chunk
        self.on_result = on_result
        self.on_step = on_step
        self.on_approval = on_approval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempts: dict[str, int] = {}
        self._procs: dict[str, object] = {}
        self._cancelled: set[str] = set()
        self._auto_paths: dict[str, str] = {}
        self._auto_lock = threading.Lock()

    def live_auto_rids(self) -> set:
        """Request ids this bridge is currently running (todo sweep skip-list)."""
        with self._auto_lock:
            return set(self._auto_paths)

    # -- lifecycle --
    def start(self) -> None:
        self._requeue_stale_claims()
        auto_todo.sweep_orphans()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="muse-bridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        self._requeue_stale_claims()
        auto_todo.sweep_orphans()
        try:
            self._loop()
        except KeyboardInterrupt:
            print("\nmuse bridge stopped.")

    def _requeue_stale_claims(self) -> None:
        """Files claimed by a dead instance never ran; put them back.

        The claim name embeds the claiming pid, so a live instance's
        in-flight tasks are never touched.
        """
        ensure_dirs()
        try:
            names = os.listdir(QUEUE_DIR)
        except OSError:
            return
        for name in names:
            base, sep, pid_s = name.rpartition(CLAIM_SUFFIX + ".")
            if not sep or not base.endswith(".json") or not pid_s.isdigit():
                continue
            try:
                alive = pid_alive(int(pid_s))
            except Exception:
                alive = False
            if alive:
                continue
            try:
                os.rename(os.path.join(QUEUE_DIR, name),
                          os.path.join(QUEUE_DIR, base))
            except OSError:
                pass

    # -- main loop --
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.settings = load_settings()
            except Exception:
                pass
            self._check_cancels()
            self._drain()
            poll = self.settings.get("tui", {}).get("poll_interval", 0.5)
            time.sleep(poll)

    def _check_cancels(self) -> None:
        ensure_dirs()
        try:
            names = os.listdir(CANCEL_DIR)
        except OSError:
            return
        for rid in names:
            _rm(os.path.join(CANCEL_DIR, rid))
            self._cancelled.add(rid)
            proc = self._procs.get(rid)
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _drain(self) -> None:
        ensure_dirs()
        if os.path.exists(PAUSED_PATH):
            return  # paused: hold the queue, keep processing cancels
        try:
            names = sorted(os.listdir(QUEUE_DIR))
        except OSError:
            return
        for name in names:
            if self._stop.is_set():
                return
            if not name.endswith(".json"):
                continue
            rid = name[:-5]
            path = os.path.join(QUEUE_DIR, name)
            # Atomic claim: the rename wins for exactly one instance, so
            # parallel TUIs (different sessions) never run the same request.
            claimed = os.path.join(
                QUEUE_DIR, f"{name}{CLAIM_SUFFIX}.{os.getpid()}")
            try:
                os.rename(path, claimed)
            except OSError:
                continue  # already claimed by another instance
            try:
                with open(claimed) as f:
                    req = json.load(f)
            except Exception:  # possibly a torn write; retry a few times
                n = self._attempts.get(rid, 0) + 1
                self._attempts[rid] = n
                if n >= MAX_PARSE_ATTEMPTS:
                    res = _cancelled_result({"id": rid})
                    res["error"] = "could not read request file"
                    res["summary"] = "could not read request file"
                    write_result(res)
                    self._attempts.pop(rid, None)
                    _rm(claimed)
                else:
                    try:
                        os.rename(claimed, path)  # release, retry next pass
                    except OSError:
                        pass
                continue
            self._attempts.pop(rid, None)
            if req.get("session", DEFAULT_SESSION) != self.session:
                # Another session's work: release the claim untouched.
                try:
                    os.rename(claimed, path)
                except OSError:
                    pass
                continue
            if req.get("ping"):
                # Liveness check (old muse-runner.py protocol): answer
                # directly, no task card and no callbacks.
                write_result({
                    "id": rid,
                    "ok": True,
                    "pong": True,
                    "task": "ping",
                    "source": req.get("source", "muse"),
                    "summary": "pong",
                })
                _rm(claimed)
                continue
            t = threading.Thread(target=self._run_one, args=(req, claimed),
                                 daemon=True,
                                 name=f"muse-task-{rid[:8]}")
            t.start()

    def _run_one(self, req: dict, claimed: str) -> None:
        rid = req.get("id", "?")
        try:
            if rid in self._cancelled:
                # Cancelled while queued: never start it.
                self._cancelled.discard(rid)
                res = _cancelled_result(req)
                write_result(res)
                _call(self.on_result, res)
                return

            if (req.get("needs_approval") and not req.get("approved")
                    and not self.settings.get("auto_approve")):
                # Park it for a human decision; the TUI approves (a) or denies (d).
                # Skipped entirely when auto_approve is on in settings.
                ensure_dirs()
                try:
                    with open(os.path.join(APPROVAL_DIR, rid + ".json"), "w") as f:
                        json.dump(req, f)
                except OSError:
                    pass
                _call(self.on_approval, req)
                return

            _call(self.on_start, req)

            # System-managed todo list: one checklist per task, created from
            # its steps (or a single item for a plain command). The bridge
            # advances it per step; agents update the same file for
            # finer-grained progress (see PROTOCOL.md).
            raw_steps = req.get("steps")
            step_names: list[str] = []
            if isinstance(raw_steps, list):
                for i, s in enumerate(raw_steps, 1):
                    if isinstance(s, dict):
                        label = (s.get("name") or s.get("label")
                                 or _short_cmd(s.get("cmd")))
                        step_names.append(label or f"step {i}")
            title = (req.get("task") or " ".join(req.get("cmd", []) or [])
                     or rid)
            auto_path = auto_todo.create(
                rid, title, step_names, owner=f"bridge:{self.session}")
            with self._auto_lock:
                self._auto_paths[rid] = auto_path

            def _chunk(line: str) -> None:
                _call(self.on_chunk, rid, line)

            def _got_proc(proc) -> None:
                self._procs[rid] = proc

            def _step(i: int, n: int, name: str) -> None:
                _call(self.on_step, rid, i, n, name)
                # on_step fires when step i *starts*: steps before it are done.
                auto_todo.advance(self._auto_paths.get(rid, ""), i - 1)

            try:
                res = run_steps(req, self.settings, on_chunk=_chunk, on_proc=_got_proc,
                                on_step=_step,
                                is_cancelled=lambda: rid in self._cancelled)
            finally:
                self._procs.pop(rid, None)

            if rid in self._cancelled:
                self._cancelled.discard(rid)
                res["ok"] = False
                res["error"] = "cancelled"
                res["summary"] = "cancelled"
                res["exit"] = None
            ok = bool(res.get("ok"))
            note = ""
            if not ok:
                note = ("cancelled" if res.get("error") == "cancelled"
                        else (res.get("summary") or res.get("error")
                              or "failed"))
            with self._auto_lock:
                done_path = self._auto_paths.pop(rid, "")
            auto_todo.finish(done_path, ok, note)
            if req.get("skills"):
                res["skills"] = list(req["skills"])
            write_result(res)
            _call(self.on_result, res)
        finally:
            _rm(claimed)

    # -- approvals --
    def pending_approvals(self) -> list[dict]:
        """Requests parked in the approval dir (e.g. across a restart).

        Only this session's requests: each instance approves its own."""
        reqs: list[dict] = []
        try:
            names = sorted(os.listdir(APPROVAL_DIR))
        except OSError:
            return []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(APPROVAL_DIR, name)) as f:
                    req = json.load(f)
            except Exception:
                continue
            if req.get("session", DEFAULT_SESSION) == self.session:
                reqs.append(req)
        return reqs

    def approve(self, rid: str) -> bool:
        """Re-queue an awaiting request. False when the id is unknown."""
        src = os.path.join(APPROVAL_DIR, rid + ".json")
        try:
            with open(src) as f:
                req = json.load(f)
        except (OSError, ValueError):
            return False
        req["approved"] = True
        try:
            with open(os.path.join(QUEUE_DIR, rid + ".json"), "w") as f:
                json.dump(req, f)
        except OSError:
            return False
        _rm(src)
        return True

    def deny(self, rid: str) -> bool:
        """Reject an awaiting request. False when the id is unknown."""
        src = os.path.join(APPROVAL_DIR, rid + ".json")
        try:
            with open(src) as f:
                req = json.load(f)
        except (OSError, ValueError):
            return False
        _rm(src)
        now = time.time()
        res = {
            "id": rid,
            "ok": False,
            "error": "denied",
            "exit": None,
            "stdout": "",
            "stderr": "",
            "truncated": False,
            "summary": "denied by user",
            "started_at": now,
            "ended_at": now,
            "duration_s": 0,
            "task": req.get("task", ""),
            "source": req.get("source", "muse"),
            "cmd": req.get("cmd", []),
            "cwd": req.get("cwd"),
        }
        write_result(res)
        _call(self.on_result, res)
        return True


def run_daemon(session: str = DEFAULT_SESSION) -> None:
    settings = load_settings()
    ensure_dirs()
    print("muse bridge online.")
    print(f"  session: {session}")
    print(f"  queue:   {QUEUE_DIR}")
    print(f"  allowed: {', '.join(sorted(settings.get('allowlist', [])))}")
    print(f"  roots:   {', '.join(settings.get('allowed_roots', [])) or '(anywhere)'}")
    print("Stop with Ctrl+C.")

    def _show(res: dict) -> None:
        task = res.get("task") or " ".join(res.get("cmd", []))
        print(f"[{res.get('source')}] {task} -> {res.get('summary')}")

    Bridge(settings, on_result=_show, session=session).run_forever()
