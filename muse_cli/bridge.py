"""Bridge: watches ~/.muse/queue, runs commands, writes ~/.muse/results.

Used two ways:
  - Headless: `python -m muse_cli --daemon` (plain stdout logging, like the
    old muse-runner.py).
  - Embedded: the TUI starts a Bridge in a background thread and wires
    on_start / on_chunk / on_result callbacks to live widgets.

Settings are reloaded every loop so editing ~/.muse/settings.json applies live.
"""
from __future__ import annotations

import json
import os
import threading
import time

from .config import load_settings
from .paths import QUEUE_DIR, ensure_dirs
from .protocol import write_result
from .runner import run_request

MAX_PARSE_ATTEMPTS = 20


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


class Bridge:
    def __init__(self, settings: dict, on_start=None, on_chunk=None, on_result=None):
        self.settings = settings
        self.on_start = on_start
        self.on_chunk = on_chunk
        self.on_result = on_result
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempts: dict[str, int] = {}

    # -- lifecycle --
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="muse-bridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        try:
            self._loop()
        except KeyboardInterrupt:
            print("\nmuse bridge stopped.")

    # -- main loop --
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.settings = load_settings()
            except Exception:
                pass
            self._drain()
            poll = self.settings.get("tui", {}).get("poll_interval", 0.5)
            time.sleep(poll)

    def _drain(self) -> None:
        ensure_dirs()
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
            try:
                with open(path) as f:
                    req = json.load(f)
            except Exception:  # possibly a torn write; retry a few times
                n = self._attempts.get(rid, 0) + 1
                self._attempts[rid] = n
                if n >= MAX_PARSE_ATTEMPTS:
                    write_result({
                        "id": rid, "ok": False, "error": "could not read request file",
                        "exit": None, "stdout": "", "stderr": "", "truncated": False,
                        "summary": "could not read request file",
                        "task": "", "source": "muse", "cmd": [], "cwd": None,
                    })
                    self._attempts.pop(rid, None)
                    _rm(path)
                continue
            self._attempts.pop(rid, None)
            _rm(path)
            t = threading.Thread(target=self._run_one, args=(req,), daemon=True,
                                 name=f"muse-task-{rid[:8]}")
            t.start()

    def _run_one(self, req: dict) -> None:
        rid = req.get("id", "?")
        if self.on_start:
            try:
                self.on_start(req)
            except Exception:
                pass

        def _chunk(line: str) -> None:
            if self.on_chunk:
                try:
                    self.on_chunk(rid, line)
                except Exception:
                    pass

        res = run_request(req, self.settings, on_chunk=_chunk)
        write_result(res)
        if self.on_result:
            try:
                self.on_result(res)
            except Exception:
                pass


def run_daemon() -> None:
    settings = load_settings()
    ensure_dirs()
    print("muse bridge online.")
    print(f"  queue:   {QUEUE_DIR}")
    print(f"  allowed: {', '.join(sorted(settings.get('allowlist', [])))}")
    print(f"  roots:   {', '.join(settings.get('allowed_roots', [])) or '(anywhere)'}")
    print("Stop with Ctrl+C.")

    def _show(res: dict) -> None:
        task = res.get("task") or " ".join(res.get("cmd", []))
        print(f"[{res.get('source')}] {task} -> {res.get('summary')}")

    Bridge(settings, on_result=_show).run_forever()
