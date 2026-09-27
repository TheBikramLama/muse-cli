"""muse-cli TUI — full-screen companion for the Muse app's terminal work.

- Scrollable task cards: each card is a unit of work (a `task` summary),
  not just a command line.
- The live line inside a running card updates *in place* (spinner, elapsed,
  streaming output tail) instead of appending lines.
- Commands stay hidden; `c` reveals a task's commands + full output.
- Every finished task carries a one-line summary of what it did.
- Bottom input box: type a command any time — while a task runs or after.
- `x` cancels the selected (or currently running) task.
- `y` copies a task's detail to the clipboard; the detail pane is a
  read-only TextArea so you can also drag-select text with the mouse.
- Status bar shows a live Muse link indicator: green "working" whenever the
  Muse app is routing work through the bridge, grey "idle" otherwise.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Footer, Header, Input, Static, TextArea

from .bridge import Bridge
from .config import load_settings
from .paths import SCRIPTS_DIR, SESSIONS_DIR, SETTINGS_PATH
from .protocol import cancel, submit
from .sessions import load_recent, log_task, new_session

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
CHUNK_THROTTLE_S = 0.15

SLASH_ALIASES = {"exit": "quit", "q": "quit", "h": "help", "resume": "sessions"}

HELP_TEXT = """\
Slash commands (type in the box below):
  /cd <dir>      change the working directory for new commands
  /run <name>    run a script from ~/.muse/scripts/
  /scripts       list scripts in ~/.muse/scripts/
  /settings      show where settings.json lives
  /sessions      list past sessions (most recent first)
  /clear         remove finished task cards
  /help          this help
  /quit          exit

Aliases: /exit = /quit, /q = /quit, /h = /help, /resume = /sessions

Keys (when the input box is not focused — press Esc to leave it):
  j/k move selection between tasks
  /   jump back to the input box
  c   show/hide the selected task's commands + full output
  x   cancel the selected (or currently running) task
  a   approve the selected task (when awaiting approval)
  d   deny the selected task (when awaiting approval)
  y   copy the selected task's detail to the clipboard
  g   jump to the newest task
  q   quit

Click a task card to select it. Anything else you type is run as a
command (split like a shell) in the session directory shown above.
"""


def copy_to_clipboard(text: str) -> bool:
    try:
        p = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                           timeout=5, capture_output=True)
        return p.returncode == 0
    except Exception:
        return False


class TaskCard(Vertical):
    """One unit of work."""

    def __init__(self, rid: str, task: str, source: str,
                 cmd: list, cwd: str | None,
                 restore_rec: dict | None = None,
                 steps: list | None = None) -> None:
        super().__init__(classes="task-card")
        self.rid = rid
        self.task_text = task or "(no description)"
        self.source = source
        self.cmd = cmd or []
        self.cwd = cwd or "~"
        self.steps = steps or []
        self._step_text = ""
        self.t0 = time.time()
        self.done = False
        self.result: dict | None = None
        self._tail: list[str] = []
        self._detail: TextArea | None = None
        # Mounting is async: compose() hasn't run until on_mount fires, so
        # anything touching composed widgets is deferred/guarded via these.
        self._composed = False
        self._restore_rec = restore_rec
        self._pending_result: dict | None = None
        self.awaiting = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task-head"):
            self.icon = Static("●", classes="ticon running")
            yield self.icon
            yield Static(self.task_text, classes="task-title")
            yield Static(self.source, classes="task-src")
            self.elapsed = Static("", classes="task-elapsed")
            yield self.elapsed
        self.live = Static("queued…", classes="live-line")
        yield self.live

    def on_click(self) -> None:
        app = self.app
        if isinstance(app, MuseCliApp):
            app.focused_rid = self.rid
            app.refresh_selection()

    def on_mount(self) -> None:
        self._composed = True
        if self._pending_result is not None:
            res, self._pending_result = self._pending_result, None
            self.finish(res)
        elif self._restore_rec is not None:
            rec, self._restore_rec = self._restore_rec, None
            self.restore(rec)
        elif self.awaiting:
            self._show_awaiting()
        elif not self.done:
            self.live.update("starting…")

    # -- live updates (invoked on the UI thread via call_from_thread) --
    def mark_running(self) -> None:
        self.awaiting = False
        if not self._composed:
            return
        self.icon.update("●")
        self.icon.remove_class("awaiting")
        self.icon.add_class("running")
        self._step_text = ""
        self._tail = []
        self.live.update("starting…")

    def mark_awaiting(self) -> None:
        self.awaiting = True
        if self._composed:
            self._show_awaiting()

    def _show_awaiting(self) -> None:
        self.icon.update("⏸")
        self.icon.remove_class("running")
        self.icon.add_class("awaiting")
        self.live.update("⏸ awaiting approval — a approve · d deny")

    def mark_approved(self) -> None:
        self.awaiting = False
        if not self._composed:
            return
        self.icon.update("●")
        self.icon.remove_class("awaiting")
        self.icon.add_class("running")
        self.live.update("approved ✓ — starting…")

    def set_step(self, i: int, n: int, name: str) -> None:
        self._step_text = f"\u25b8 {i}/{n} \u00b7 {name}"
        self._render_live()

    def _render_live(self) -> None:
        if not self._composed:
            return
        parts = [self._step_text] if self._step_text else []
        parts.extend(self._tail)
        self.live.update("\n".join(parts) if parts else "\u2026")

    def push_chunk(self, line: str) -> None:
        if not self._composed:
            return
        line = line.rstrip()
        if not line:
            return
        self._tail.append(line)
        self._tail = self._tail[-3:]
        self._render_live()

    def tick(self, frame: int) -> None:
        if self.done or not self._composed:
            return
        self.icon.update(SPINNER[frame % len(SPINNER)])
        self.elapsed.update(f"{time.time() - self.t0:.0f}s")

    def finish(self, res: dict) -> None:
        if not self._composed:
            # Mount not processed yet (instant task); on_mount applies it.
            self._pending_result = res
            return
        self.done = True
        self.awaiting = False
        self.result = res
        ok = bool(res.get("ok")) and res.get("exit", 1) == 0
        self.icon.update("✓" if ok else "✗")
        self.icon.remove_class("running")
        self.icon.add_class("done" if ok else "failed")
        self.elapsed.update(f"{res.get('duration_s', 0):.1f}s")
        self.live.update(res.get("summary") or res.get("error") or "done")
        self._detail = TextArea(self._detail_text(res), read_only=True,
                                classes="detail")
        self._detail.display = False
        self.mount(self._detail)

    def restore(self, rec: dict) -> None:
        """Rebuild a finished card from session history."""
        self.done = True
        ok = bool(rec.get("ok")) and rec.get("exit", 1) == 0
        self.icon.update("✓" if ok else "✗")
        self.icon.remove_class("running")
        self.icon.add_class("done" if ok else "failed")
        self.elapsed.update(f"{rec.get('duration_s', 0):.1f}s")
        self.live.update(rec.get("summary") or "")
        self._detail = TextArea(
            f"$ {' '.join(self.cmd)}\nin {self.cwd} · {self.source} · previous session\n"
            f"summary: {rec.get('summary') or ''}",
            read_only=True, classes="detail")
        self._detail.display = False
        self.mount(self._detail)

    def _detail_text(self, res: dict) -> str:
        if self.steps:
            first = f"{len(self.steps)} step(s): " + "; ".join(
                str(s.get("name", "")) for s in self.steps)
        else:
            first = f"$ {' '.join(self.cmd)}"
        lines = [
            first,
            f"in {self.cwd} · {self.source} · {res.get('duration_s', 0):.1f}s",
            f"summary: {res.get('summary') or res.get('error') or ''}",
        ]
        if res.get("steps"):
            lines.append("")
            for s in res["steps"]:
                mark = "✓" if s.get("ok") and (s.get("exit") or 0) == 0 else "✗"
                lines.append(f"  {mark} {s.get('name')} · {s.get('summary', '')}")
        lines += ["", "--- stdout ---", res.get("stdout") or "(empty)"]
        if res.get("stderr"):
            lines += ["", "--- stderr ---", res["stderr"]]
        if res.get("truncated"):
            lines += ["", "[output truncated]"]
        return "\n".join(lines)

    def toggle_detail(self) -> bool:
        if self._detail is None:
            return False
        self._detail.display = not self._detail.display
        return True

    def detail_text(self) -> str:
        if self._detail is not None:
            return self._detail.text
        if self.result is not None:
            return self._detail_text(self.result)
        return self.task_text


class MuseCliApp(App):
    TITLE = "muse-cli"
    CSS = """
    #statusbar {
        dock: top; height: 1;
        background: $surface; color: $text-muted;
        padding: 0 1;
    }
    #tasks { height: 1fr; }
    #cmd { dock: bottom; margin: 0 1 1 1; }
    .task-card {
        border: solid $primary-darken-2;
        margin: 0 1 1 1; padding: 0 1;
        height: auto;
    }
    .task-card.focused { border: solid $accent; }
    .task-head { height: auto; }
    .ticon { width: 3; color: $warning; }
    .ticon.done { color: $success; }
    .ticon.failed { color: $error; }
    .ticon.awaiting { color: $warning; }
    .task-title { width: 1fr; text-style: bold; }
    .task-src { width: auto; color: $text-muted; margin-left: 1; }
    .task-elapsed { width: auto; color: $text-muted; margin-left: 1; }
    .live-line { color: $text-muted; height: auto; }
    .detail { height: 16; border-top: solid $surface-lighten-2; margin-top: 1; }
    .help-card {
        border: solid $surface-lighten-2;
        margin: 0 1 1 1; padding: 1;
        height: auto; color: $text;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "toggle_detail", "Commands"),
        ("x", "cancel_task", "Cancel"),
        ("a", "approve_task", "Approve"),
        ("d", "deny_task", "Deny"),
        ("y", "copy_task", "Copy"),
        ("g", "scroll_bottom", "Bottom"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.session_id = new_session()
        self.session_cwd = os.getcwd()
        self.cards: dict[str, TaskCard] = {}
        self.focused_rid: str | None = None
        self._frame = 0
        self._last_chunk: dict[str, float] = {}
        self._muse_running = 0
        self._bridge: Bridge | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.statusbar = Static("", id="statusbar")
        yield self.statusbar
        self.task_list = ScrollableContainer(id="tasks")
        yield self.task_list
        yield Input(placeholder="type a command, Enter to run · Esc for keys · /help", id="cmd")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_statusbar("bridge starting…")
        for rec in load_recent(self.settings.get("tui", {}).get("history_limit", 50)):
            card = TaskCard(rec.get("id", "?"), rec.get("task", ""),
                            rec.get("source", "muse"), rec.get("cmd", []),
                            rec.get("cwd"), restore_rec=rec)
            self.cards[card.rid] = card
            self.task_list.mount(card)
        self._bridge = Bridge(
            self.settings,
            on_start=self._cb_start,
            on_chunk=self._cb_chunk,
            on_result=self._cb_result,
            on_step=self._cb_step,
            on_approval=self._cb_approval,
        )
        self._bridge.start()
        for req in self._bridge.pending_approvals():
            self._on_approval(req)
        self._refresh_statusbar("bridge online")
        self.set_interval(0.1, self._tick)
        self.task_list.scroll_end(animate=False)
        try:
            self.query_one("#cmd", Input).focus()
        except Exception:
            pass

    def on_unmount(self) -> None:
        if self._bridge is not None:
            self._bridge.stop()

    # -- bridge callbacks (run on worker threads) --
    def _safe_call(self, fn, *args) -> None:
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:
            # Already on the app thread (e.g. deny() triggered by a key
            # action runs the bridge callback synchronously): call directly.
            if self.is_running:
                try:
                    fn(*args)
                except Exception:
                    pass
        except Exception:
            pass

    def _cb_start(self, req: dict) -> None:
        self._safe_call(self._on_start, req)

    def _cb_chunk(self, rid: str, line: str) -> None:
        now = time.monotonic()
        if now - self._last_chunk.get(rid, 0) < CHUNK_THROTTLE_S:
            return
        self._last_chunk[rid] = now
        self._safe_call(self._on_chunk, rid, line)

    def _cb_result(self, res: dict) -> None:
        self._safe_call(self._on_result, res)

    def _cb_step(self, rid: str, i: int, n: int, name: str) -> None:
        self._safe_call(self._on_step, rid, i, n, name)

    def _cb_approval(self, req: dict) -> None:
        self._safe_call(self._on_approval, req)

    # -- UI-thread handlers --
    def _on_start(self, req: dict) -> None:
        rid = req["id"]
        if req.get("source") == "muse":
            self._muse_running += 1
            self._refresh_statusbar()
        card = self.cards.get(rid)
        if card is None:
            # Submitted externally (e.g. by Muse dropping a file in the queue).
            card = TaskCard(rid, req.get("task", ""), req.get("source", "muse"),
                            req.get("cmd", []), req.get("cwd"),
                            steps=req.get("steps"))
            self.cards[rid] = card
            self.focused_rid = rid
            self.task_list.mount(card)
            self.task_list.scroll_end(animate=False)
            self.refresh_selection()
        card.mark_running()

    def _on_chunk(self, rid: str, line: str) -> None:
        card = self.cards.get(rid)
        if card is not None:
            card.push_chunk(line)

    def _on_step(self, rid: str, i: int, n: int, name: str) -> None:
        card = self.cards.get(rid)
        if card is not None:
            card.set_step(i, n, name)

    def _on_approval(self, req: dict) -> None:
        rid = req["id"]
        card = self.cards.get(rid)
        if card is None:
            card = TaskCard(rid, req.get("task", ""), req.get("source", "muse"),
                            req.get("cmd", []), req.get("cwd"),
                            steps=req.get("steps"))
            self.cards[rid] = card
            self.focused_rid = rid
            self.task_list.mount(card)
            self.task_list.scroll_end(animate=False)
            self.refresh_selection()
        card.mark_awaiting()
        self.notify(f"approval needed: {card.task_text[:50]}")

    def _on_result(self, res: dict) -> None:
        if res.get("source") == "muse":
            self._muse_running = max(0, self._muse_running - 1)
            self._refresh_statusbar()
        card = self.cards.get(res["id"])
        if card is not None:
            card.finish(res)
        log_task(self.session_id, res)
        self.task_list.scroll_end(animate=False)

    def _tick(self) -> None:
        self._frame += 1
        for card in self.cards.values():
            card.tick(self._frame)

    # -- input box --
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            self._slash(text)
            return
        try:
            cmd = shlex.split(text)
        except ValueError as e:
            self.notify(f"could not parse: {e}")
            return
        if not cmd:
            return
        rid = submit(task=text[:80], cmd=cmd, cwd=self.session_cwd, source="local")
        card = TaskCard(rid, text[:80], "local", cmd, self.session_cwd)
        self.cards[rid] = card
        self.focused_rid = rid
        self.task_list.mount(card)
        self.task_list.scroll_end(animate=False)
        self.refresh_selection()

    def _slash(self, text: str) -> None:
        parts = text[1:].split(None, 1)
        name = SLASH_ALIASES.get(parts[0].lower(), parts[0].lower())
        arg = parts[1] if len(parts) > 1 else ""
        if name == "cd":
            d = os.path.abspath(os.path.expanduser(arg or "~"))
            if os.path.isdir(d):
                self.session_cwd = d
                self._refresh_statusbar(f"cwd → {d}")
            else:
                self.notify(f"no such directory: {arg or '~'}")
        elif name == "clear":
            for rid, card in list(self.cards.items()):
                if card.done:
                    card.remove()
                    del self.cards[rid]
            self.focused_rid = None
        elif name == "help":
            self.task_list.mount(Static(HELP_TEXT, classes="help-card"))
            self.task_list.scroll_end(animate=False)
        elif name == "scripts":
            try:
                names = sorted(os.listdir(SCRIPTS_DIR))
            except OSError:
                names = []
            self.notify("scripts: " + (", ".join(names) if names else "(none) — add to ~/.muse/scripts/"))
        elif name == "run" and arg:
            p = os.path.join(SCRIPTS_DIR, arg)
            if not os.path.isfile(p):
                self.notify(f"no such script: {arg}")
                return
            rid = submit(task=f"run script: {arg}", cmd=[p],
                         cwd=self.session_cwd, source="local")
            card = TaskCard(rid, f"run script: {arg}", "local", [p], self.session_cwd)
            self.cards[rid] = card
            self.focused_rid = rid
            self.task_list.mount(card)
            self.task_list.scroll_end(animate=False)
            self.refresh_selection()
        elif name == "settings":
            self.notify(f"settings: {SETTINGS_PATH}")
        elif name == "sessions":
            self._list_sessions()
        elif name in ("quit", "q"):
            self.exit()
        else:
            self.notify(f"unknown command: /{name}  (try /help)")

    def _list_sessions(self) -> None:
        try:
            files = sorted(
                (f for f in os.listdir(SESSIONS_DIR) if f.endswith(".jsonl")),
                key=lambda f: os.path.getmtime(os.path.join(SESSIONS_DIR, f)),
                reverse=True,
            )
        except OSError:
            files = []
        if not files:
            self.notify("no previous sessions")
            return
        lines = ["sessions (most recent first):"]
        for f in files[:10]:
            p = os.path.join(SESSIONS_DIR, f)
            try:
                with open(p) as fh:
                    n = sum(1 for _ in fh)
            except OSError:
                n = 0
            marker = "  ← current" if f.startswith(self.session_id) else ""
            lines.append(f"  {f} · {n} task(s){marker}")
        self.task_list.mount(Static("\n".join(lines), classes="help-card"))
        self.task_list.scroll_end(animate=False)

    def on_key(self, event: events.Key) -> None:
        """Keyboard-first navigation.

        Single-letter bindings only fire when the input box is NOT focused
        (Textual gives typed characters to the focused Input). Esc leaves
        the input so the keys work; / jumps back into it; j/k move the
        selection between task cards.
        """
        in_input = isinstance(self.focused, Input)
        if event.key == "escape" and in_input:
            self.query_one("#cmd", Input).blur()
            event.prevent_default()
        elif event.key == "slash" and not in_input:
            self.query_one("#cmd", Input).focus()
            event.prevent_default()
        elif event.key in ("j", "down") and not in_input:
            self._move_selection(1)
            event.prevent_default()
        elif event.key in ("k", "up") and not in_input:
            self._move_selection(-1)
            event.prevent_default()

    def _move_selection(self, delta: int) -> None:
        rids = [c.rid for c in self.task_list.query(TaskCard)]
        if not rids:
            return
        try:
            i = rids.index(self.focused_rid)
        except ValueError:
            i = -1 if delta > 0 else 0
        i = max(0, min(len(rids) - 1, i + delta))
        self.focused_rid = rids[i]
        self.refresh_selection()
        card = self.cards.get(self.focused_rid)
        if card is not None:
            card.scroll_visible()

    # -- selection, keybindings, status --
    def refresh_selection(self) -> None:
        for rid, card in self.cards.items():
            if rid == self.focused_rid:
                card.add_class("focused")
            else:
                card.remove_class("focused")

    def action_toggle_detail(self) -> None:
        card = self.cards.get(self.focused_rid or "")
        if card is None or not card.toggle_detail():
            self.notify("nothing to show yet")
            return
        assert card._detail is not None
        self.notify("commands shown" if card._detail.display else "commands hidden")

    def _approval_card(self):
        card = self.cards.get(self.focused_rid or "")
        if card is None or not card.awaiting:
            self.notify("no task awaiting approval")
            return None
        if self._bridge is None:
            self.notify("bridge not running")
            return None
        return card

    def action_approve_task(self) -> None:
        card = self._approval_card()
        if card is None or self._bridge is None:
            return
        if self._bridge.approve(card.rid):
            card.mark_approved()
            self.notify("approved — queued")
        else:
            self.notify("approval expired")

    def action_deny_task(self) -> None:
        card = self._approval_card()
        if card is None or self._bridge is None:
            return
        if self._bridge.deny(card.rid):
            self.notify("denied")
        else:
            self.notify("approval expired")

    def action_cancel_task(self) -> None:
        card = self.cards.get(self.focused_rid or "")
        if card is None or card.done:
            running = [c for c in self.cards.values() if not c.done]
            card = running[0] if running else None
        if card is None:
            self.notify("nothing running")
            return
        cancel(card.rid)
        self.notify(f"cancelling: {card.task_text[:40]}")

    def action_copy_task(self) -> None:
        card = self.cards.get(self.focused_rid or "")
        if card is None:
            self.notify("no task selected")
            return
        if copy_to_clipboard(card.detail_text()):
            self.notify("copied to clipboard")
        else:
            self.notify("copy failed")

    def action_scroll_bottom(self) -> None:
        self.task_list.scroll_end(animate=False)

    def _refresh_statusbar(self, extra: str = "") -> None:
        t = Text()
        t.append("muse-cli · cwd: ")
        t.append(self.session_cwd)
        t.append(" · bridge ")
        t.append("●", style="green")
        t.append(" · muse ")
        if self._muse_running:
            t.append("● working", style="green")
        else:
            t.append("○ idle", style="dim")
        if extra:
            t.append(f" · {extra}")
        self.statusbar.update(t)
