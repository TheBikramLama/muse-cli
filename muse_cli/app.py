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
import json
import re
import subprocess
import threading
import time

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Footer, Header, Input, Markdown, Static, TextArea

from .bridge import Bridge
from .config import load_settings, save_settings
from .paths import (EXPORTS_DIR, INPUT_HISTORY_PATH, MESSAGES_DIR, PAUSED_PATH,
                   REPLIES_DIR, SCRIPTS_DIR, SEEN_PATH, SESSIONS_DIR,
                   SETTINGS_PATH, TODOS_DIR, ensure_dirs)
from .protocol import cancel, new_id, submit
from .runner import check_cwd
from .sessions import load_recent, log_task, new_session

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
CHUNK_THROTTLE_S = 0.15

SLASH_ALIASES = {"exit": "quit", "q": "quit", "h": "help", "resume": "sessions"}

HELP_TEXT = """\
Input modes (type in the box below):
  /command     TUI commands — /help, /cd, /run, /skills, /todos, ...
  !shell cmd   run a shell command right here (pipes, &&, etc. all work)
  plain text   send a message to Muse (the app) — replies appear here

Slash commands:
  /cd <dir>      change the working directory for new commands
  /run <name>    run a script from ~/.muse/scripts/
  /scripts       list scripts in ~/.muse/scripts/
  /skills        list installed skills (● = in use by a running task)
  /skill install <git-url|path>   install a Claude-compatible skill
  /skill remove <name>            remove a skill
  /skill show <name>              read a skill's SKILL.md
  /todos         list your todo lists
  /todo <name>   jump to a todo list
  /autoapprove [on|off]  toggle auto-approval of approval requests
  /settings      show where settings.json lives
  /sessions      list past sessions (most recent first)
  /session <name> switch the session new tasks are logged to
  /export [name]  save this session's finished tasks as markdown
  /clear         remove finished task cards
  /help          this help
  /quit          exit

Aliases: /exit = /quit, /q = /quit, /h = /help, /resume = /sessions

Keys (when the input box is not focused — press Esc to leave it):
  j/k move selection between tasks
  /   jump back to the input box
  c   show/hide the selected task's commands + full output
  x   cancel the selected (or currently running) task
  p   pause/resume the bridge queue
  r   retry the selected finished task
  a   approve the selected task (when awaiting approval)
  d   deny the selected task (when awaiting approval)
  A   toggle auto-approve for approval requests
  y   copy the selected task's detail to the clipboard
  s   save the selected task's detail + output to ~/.muse/exports/
  g   jump to the newest task
  q   quit

Click a task card to select it. Plain text you type is a message to Muse;
prefix with ! to run it as a shell command instead.
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


class MessageCard(Vertical):
    """A chat message — yours (outgoing) or Muse's reply (incoming)."""

    def __init__(self, mid: str, text: str, incoming: bool,
                 at: float | None = None) -> None:
        super().__init__(classes="msg-card" + (" incoming" if incoming else ""))
        self.mid = mid
        self.incoming = incoming
        self.at = at or time.time()
        self._text = text

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task-head"):
            yield Static("💬" if self.incoming else "🗨", classes="ticon done")
            who = "Muse" if self.incoming else "you"
            yield Static(who, classes="task-title")
            yield Static(time.strftime("%H:%M", time.localtime(self.at)),
                         classes="task-src")
        yield Markdown(self._text, classes="msg-body")


class TodoCard(Vertical):
    """A live markdown checklist from ~/.muse/todos/<name>.md.

    The file is the source of truth: edit it (or let Muse edit it) and
    the card re-renders with fresh checkboxes and n/m progress.
    """

    def __init__(self, name: str) -> None:
        super().__init__(classes="todo-card")
        self.tname = name
        self.path = os.path.join(TODOS_DIR, name)
        self._mtime = 0.0
        self._composed = False
        self._pending_text: str | None = None
        self.progress_text = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task-head"):
            yield Static("☑", classes="ticon done")
            title = self.tname[:-3] if self.tname.endswith(".md") else self.tname
            yield Static(title, classes="task-title")
            self.progress = Static("", classes="task-src")
            yield self.progress
        self.body = Markdown("", classes="msg-body")
        yield self.body

    def on_mount(self) -> None:
        self._composed = True
        if self._pending_text is not None:
            text, self._pending_text = self._pending_text, None
            self._apply_text(text)

    def refresh_if_changed(self) -> bool:
        """Re-render when the file changed. False when the file is gone."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime == self._mtime and self._composed:
            return True
        self._mtime = mtime
        try:
            with open(self.path) as f:
                text = f.read()
        except OSError:
            return False
        if not self._composed:
            self._pending_text = text
            return True
        self._apply_text(text)
        return True

    def _apply_text(self, text: str) -> None:
        done = len(re.findall(r"^\s*[-*]\s+\[x\]", text, re.M | re.I))
        open_ = len(re.findall(r"^\s*[-*]\s+\[ \]", text, re.M))
        total = done + open_
        self.progress_text = f"{done}/{total}" if total else "empty"
        if self._composed:
            self.progress.update(self.progress_text)
        # Real checkboxes regardless of the markdown renderer's task-list support.
        pretty = re.sub(r"^(\s*[-*]\s+)\[ \]", r"\1☐", text, flags=re.M)
        pretty = re.sub(r"^(\s*[-*]\s+)\[x\]", r"\1☑", pretty, flags=re.M | re.I)
        if self._composed:
            try:
                self.body.update(pretty)
            except Exception:
                pass


class MuseCliApp(App):
    TITLE = "muse-cli"
    CSS = """
    #statusbar {
        dock: top; height: 1;
        background: $surface; color: $text-muted;
        padding: 0 1;
    }
    #tasks { height: 1fr; }
    #empty {
        text-align: center;
        color: $text-muted;
        padding: 2 1;
    }
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
    .msg-card {
        border: solid $surface-lighten-2;
        margin: 0 1 1 1; padding: 0 1;
        height: auto;
    }
    .msg-card.incoming { border: solid $primary-darken-2; }
    .msg-body { height: auto; }
    .todo-card {
        border: solid $accent-darken-2;
        margin: 0 1 1 1; padding: 0 1;
        height: auto;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "toggle_detail", "Commands"),
        ("x", "cancel_task", "Cancel"),
        ("a", "approve_task", "Approve"),
        ("d", "deny_task", "Deny"),
        ("A", "toggle_auto_approve", "Auto-approve"),
        ("p", "toggle_pause", "Pause"),
        ("r", "retry_task", "Retry"),
        ("s", "save_output", "Save"),
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
        self._history: list[str] = self._load_history()
        self._hist_idx: int | None = None
        self._hist_draft = ""
        self._empty_shown = True
        self._last_active = 0
        self._shell_procs: dict[str, object] = {}
        self._seen_replies: set[str] = self._load_seen()
        self._poll_n = 0
        self._todo_cards: dict[str, TodoCard] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        self.statusbar = Static("", id="statusbar")
        yield self.statusbar
        self.task_list = ScrollableContainer(id="tasks")
        yield self.task_list
        yield Input(placeholder="/ command · ! shell · text = message to Muse", id="cmd")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_statusbar("bridge starting…")
        self.empty_state = Static(
            "no tasks yet — /help for commands · ! for shell · plain text messages Muse",
            id="empty")
        self.task_list.mount(self.empty_state)
        for rec in load_recent(self.settings.get("tui", {}).get("history_limit", 50)):
            card = TaskCard(rec.get("id", "?"), rec.get("task", ""),
                            rec.get("source", "muse"), rec.get("cmd", []),
                            rec.get("cwd"), restore_rec=rec)
            self.cards[card.rid] = card
            self.task_list.mount(card)
        self._render_message_history()
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
        if self.settings.get("tui", {}).get("notify_on_done"):
            self._notify_done(res)

    def _task_counts(self) -> tuple[int, int]:
        """(running/active, awaiting-approval) card counts."""
        active = sum(1 for c in self.cards.values()
                     if not c.done and not c.awaiting)
        waiting = sum(1 for c in self.cards.values() if c.awaiting)
        return active, waiting

    def _tick(self) -> None:
        self._frame += 1
        for card in self.cards.values():
            card.tick(self._frame)
        show_empty = not self.cards and not self._todo_cards
        if show_empty != self._empty_shown:
            self._empty_shown = show_empty
            self.empty_state.display = show_empty
        active, _ = self._task_counts()
        if active != self._last_active:
            self._last_active = active
            self._refresh_statusbar()
        self._poll_n += 1
        if self._poll_n % 10 == 0:  # ~1s
            self._poll_replies()
            self._poll_todos()

    # -- messages: ~/.muse/messages (you -> Muse) / ~/.muse/replies (Muse -> you)
    @staticmethod
    def _load_seen() -> set[str]:
        try:
            with open(SEEN_PATH) as f:
                ids = json.load(f)
            return {x for x in ids if isinstance(x, str)}
        except (OSError, ValueError):
            return set()

    def _save_seen(self) -> None:
        try:
            with open(SEEN_PATH, "w") as f:
                json.dump(sorted(self._seen_replies), f)
        except OSError:
            pass

    def _poll_replies(self) -> None:
        try:
            names = sorted(os.listdir(REPLIES_DIR))
        except OSError:
            return
        new = False
        for name in names:
            if not name.endswith(".json"):
                continue
            mid = name[:-5]
            if mid in self._seen_replies:
                continue
            try:
                with open(os.path.join(REPLIES_DIR, name)) as f:
                    payload = json.load(f)
            except (OSError, ValueError):
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            card = MessageCard(mid, text, incoming=True,
                               at=payload.get("at") or time.time())
            self.task_list.mount(card)
            self.task_list.scroll_end(animate=False)
            self._seen_replies.add(mid)
            new = True
            self.notify("💬 reply from Muse")
        if new:
            self._save_seen()

    # -- todos: ~/.muse/todos/*.md watched live --
    def _poll_todos(self) -> None:
        ensure_dirs()
        try:
            names = sorted(f for f in os.listdir(TODOS_DIR)
                           if f.endswith(".md"))
        except OSError:
            names = []
        for name in names:
            card = self._todo_cards.get(name)
            if card is None:
                card = TodoCard(name)
                self._todo_cards[name] = card
                self.task_list.mount(card)
                self.task_list.scroll_end(animate=False)
            if not card.refresh_if_changed():
                self._drop_todo(name)
        for name in list(self._todo_cards):
            if name not in names:
                self._drop_todo(name)

    def _drop_todo(self, name: str) -> None:
        card = self._todo_cards.pop(name, None)
        if card is not None:
            try:
                card.remove()
            except Exception:
                pass

    def _list_todos(self) -> None:
        if not self._todo_cards:
            self.notify("no todo lists — write markdown to ~/.muse/todos/")
            return
        lines = ["todo lists:"]
        for name in sorted(self._todo_cards):
            card = self._todo_cards[name]
            lines.append(f"  {name} · {card.progress_text or '?'}")
        self.task_list.mount(Static("\n".join(lines), classes="help-card"))
        self.task_list.scroll_end(animate=False)

    def _render_message_history(self) -> None:
        """Show recent messages/replies as history on startup (max 30)."""
        items: list[tuple[float, bool, str, str]] = []
        for d, incoming in ((MESSAGES_DIR, False), (REPLIES_DIR, True)):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(d, name)) as f:
                        p = json.load(f)
                    t = p.get("text")
                    if isinstance(t, str) and t.strip():
                        items.append((p.get("at") or 0, incoming,
                                      name[:-5], t))
                except (OSError, ValueError):
                    continue
        items.sort(key=lambda x: x[0])
        for at, incoming, mid, text in items[-30:]:
            if incoming:
                self._seen_replies.add(mid)
            self.task_list.mount(MessageCard(mid, text, incoming, at))
        if items:
            self._save_seen()

    # -- input box --
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        self._hist_push(text)
        if text.startswith("/"):
            self._slash(text)
            return
        if text.startswith("!"):
            self._shell(text[1:].strip())
            return
        self._send_message(text)

    def _shell(self, cmd_text: str) -> None:
        """Run a shell command right here in the TUI (bash -lc)."""
        if not cmd_text:
            self.notify("usage: !<shell command>")
            return
        cwd, err = check_cwd(self.session_cwd,
                             self.settings.get("allowed_roots", ["~"]))
        if err:
            self.notify(err)
            return
        rid = new_id()
        task = f"! {cmd_text[:80]}"
        card = TaskCard(rid, task, "shell", ["bash", "-lc", cmd_text], cwd)
        self.cards[rid] = card
        self.focused_rid = rid
        self.task_list.mount(card)
        self.task_list.scroll_end(animate=False)
        self.refresh_selection()

        def _run() -> None:
            t0 = time.time()
            try:
                proc = subprocess.Popen(
                    ["bash", "-lc", cmd_text], cwd=cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", bufsize=1,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
            except Exception as e:
                res = self._shell_result(rid, task, cwd, cmd_text, t0,
                                         ok=False, exit_code=None,
                                         output="", error=str(e))
                self.call_from_thread(self._shell_done, rid, res)
                return
            self._shell_procs[rid] = proc
            chunks: list[str] = []
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    chunks.append(line)
                    self.call_from_thread(self._shell_chunk, rid, line)
                proc.wait(timeout=self.settings.get("default_timeout", 120))
                exit_code = proc.returncode
                error = ""
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = None
                error = "timed out"
            except Exception as e:  # cancelled via x -> proc killed
                exit_code = None
                error = str(e) or "cancelled"
            finally:
                self._shell_procs.pop(rid, None)
            res = self._shell_result(rid, task, cwd, cmd_text, t0,
                                     ok=not error and exit_code == 0,
                                     exit_code=exit_code,
                                     output="".join(chunks), error=error)
            try:
                self.call_from_thread(self._shell_done, rid, res)
            except RuntimeError:
                if self.is_running:
                    self._shell_done(rid, res)

        threading.Thread(target=_run, daemon=True,
                         name=f"muse-shell-{rid[:8]}").start()

    @staticmethod
    def _shell_result(rid: str, task: str, cwd: str, cmd_text: str, t0: float,
                      ok: bool, exit_code: int | None,
                      output: str, error: str) -> dict:
        ended = time.time()
        lines = [l for l in output.splitlines() if l.strip()]
        tail = lines[-1][:120] if lines else "no output"
        summary = (f"ok · {len(lines)} line(s) · {tail}" if ok
                   else f"{error or f'exit {exit_code}'}")
        return {
            "id": rid, "ok": ok, "exit": exit_code,
            "stdout": output, "stderr": "", "truncated": False,
            "summary": summary, "started_at": t0, "ended_at": ended,
            "duration_s": round(ended - t0, 1), "task": task,
            "source": "shell", "cmd": ["bash", "-lc", cmd_text], "cwd": cwd,
            "error": error,
        }

    def _shell_chunk(self, rid: str, line: str) -> None:
        card = self.cards.get(rid)
        if card is not None:
            card.push_chunk(line)

    def _shell_done(self, rid: str, res: dict) -> None:
        card = self.cards.get(rid)
        if card is not None:
            card.finish(res)
        log_task(self.session_id, res)
        self.task_list.scroll_end(animate=False)
        if self.settings.get("tui", {}).get("notify_on_done"):
            self._notify_done(res)

    def _send_message(self, text: str) -> None:
        """Plain text is a message to the Muse app (not a command)."""
        ensure_dirs()
        mid = new_id()
        payload = {"id": mid, "from": "tui", "text": text,
                   "at": time.time()}
        tmp = os.path.join(MESSAGES_DIR, mid + ".json.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, os.path.join(MESSAGES_DIR, mid + ".json"))
        except OSError:
            self.notify("could not send message")
            return
        card = MessageCard(mid, text, incoming=False, at=payload["at"])
        self.task_list.mount(card)
        self.task_list.scroll_end(animate=False)
        self.notify("✉ sent to Muse — replies appear here")

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
        elif name == "session":
            if not arg:
                self.notify(f"current session: {self.session_id}")
            elif not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", arg):
                self.notify("session name: letters, digits, _ and - only")
            else:
                self.session_id = arg
                self._refresh_statusbar()
                self.notify(f"session → {arg} (new tasks log here)")
        elif name == "todos":
            self._list_todos()
        elif name == "todo":
            if not arg:
                self._list_todos()
            else:
                key = arg if arg.endswith(".md") else arg + ".md"
                card = self._todo_cards.get(key)
                if card is None:
                    self.notify(f"no todo list: {arg} (see /todos)")
                else:
                    card.scroll_visible()
                    self.notify(f"todo: {key} · {card.progress_text}")
        elif name == "autoapprove":
            if not arg:
                self._set_auto_approve(not self.settings.get("auto_approve", False))
            elif arg == "on":
                self._set_auto_approve(True)
            elif arg == "off":
                self._set_auto_approve(False)
            else:
                self.notify("usage: /autoapprove [on|off]")
        elif name == "export":
            self._export_session(arg)
        elif name in ("quit", "q"):
            self.exit()
        else:
            self.notify(f"unknown command: /{name}  (try /help)")

    def _export_session(self, arg: str) -> None:
        cards = [c for c in self.task_list.query(TaskCard)
                 if c.done and c.result is not None]
        if not cards:
            self.notify("nothing to export")
            return
        name = re.sub(r"[^A-Za-z0-9_-]", "-", arg.strip() or
                      f"session-{self.session_id}")[:60] or "session"
        ensure_dirs()
        path = os.path.join(EXPORTS_DIR, f"{name}.md")
        lines = [f"# muse-cli session {self.session_id}", ""]
        for card in cards:
            res = card.result or {}
            lines.append(f"## {card.task_text}")
            lines.append("")
            lines.append(
                f"- source: {card.source} · cwd: {card.cwd} · "
                f"{res.get('duration_s', 0):.1f}s · "
                f"{res.get('summary') or res.get('error') or ''}")
            if card.steps:
                lines.append(
                    f"- {len(card.steps)} step(s): " +
                    "; ".join(str(s.get("name", "")) for s in card.steps))
            elif card.cmd:
                lines.append(f"- `$ {' '.join(card.cmd)}`")
            out = (res.get("stdout") or "").rstrip()
            if out:
                lines += ["", "```", out, "```"]
            err = (res.get("stderr") or "").rstrip()
            if err:
                lines += ["", "stderr:", "```", err, "```"]
            lines.append("")
        try:
            with open(path, "w") as f:
                f.write("\n".join(lines))
        except OSError:
            self.notify("could not export")
            return
        self.notify(f"exported {len(cards)} task(s): {path}")

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
        elif event.key == "up" and in_input:
            self._hist_move(-1)
            event.prevent_default()
        elif event.key == "down" and in_input:
            self._hist_move(1)
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

    # -- input history (↑/↓ in the box) --
    @staticmethod
    def _load_history() -> list[str]:
        try:
            with open(INPUT_HISTORY_PATH) as f:
                h = json.load(f)
            return [x for x in h if isinstance(x, str)][-200:]
        except (OSError, ValueError):
            return []

    def _hist_push(self, text: str) -> None:
        if not (self._history and self._history[-1] == text):
            self._history.append(text)
            self._history = self._history[-200:]
            try:
                with open(INPUT_HISTORY_PATH, "w") as f:
                    json.dump(self._history, f)
            except OSError:
                pass
        self._hist_idx = None

    def _hist_move(self, delta: int) -> None:
        if not self._history:
            return
        inp = self.query_one("#cmd", Input)
        if self._hist_idx is None:
            self._hist_draft = inp.value
            self._hist_idx = len(self._history)
        self._hist_idx = max(0, min(len(self._history), self._hist_idx + delta))
        if self._hist_idx < len(self._history):
            inp.value = self._history[self._hist_idx]
        else:
            inp.value = self._hist_draft

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

    def action_toggle_auto_approve(self) -> None:
        self._set_auto_approve(not self.settings.get("auto_approve", False))

    def _set_auto_approve(self, on: bool) -> None:
        self.settings["auto_approve"] = on
        try:
            save_settings(self.settings)
        except OSError:
            self.notify("could not save settings")
            return
        self._refresh_statusbar()
        self.notify(f"auto-approve {'ON ⚡' if on else 'OFF'} — "
                    + ("approval requests run immediately"
                       if on else "approval requests park for a/d"))

    def action_toggle_pause(self) -> None:
        if os.path.exists(PAUSED_PATH):
            try:
                os.remove(PAUSED_PATH)
            except OSError:
                pass
            self.notify("bridge resumed — queue flowing")
        else:
            try:
                with open(PAUSED_PATH, "w") as f:
                    f.write("")
            except OSError:
                self.notify("could not pause")
                return
            self.notify("bridge paused — queue held")
        self._refresh_statusbar()

    def action_retry_task(self) -> None:
        card = self.cards.get(self.focused_rid or "")
        if card is None or not card.done:
            self.notify("select a finished task to retry")
            return
        rid = submit(task=card.task_text, cmd=card.cmd, cwd=card.cwd,
                     source="local", steps=card.steps or None)
        new_card = TaskCard(rid, card.task_text, "local", card.cmd,
                            card.cwd, steps=card.steps)
        self.cards[rid] = new_card
        self.focused_rid = rid
        self.task_list.mount(new_card)
        self.task_list.scroll_end(animate=False)
        self.refresh_selection()
        self.notify("retried")

    def action_cancel_task(self) -> None:
        card = self.cards.get(self.focused_rid or "")
        if card is None or card.done:
            running = [c for c in self.cards.values() if not c.done]
            card = running[0] if running else None
        if card is None:
            self.notify("nothing running")
            return
        proc = self._shell_procs.get(card.rid)
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            self.notify(f"cancelling: {card.task_text[:40]}")
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

    def action_save_output(self) -> None:
        card = self.cards.get(self.focused_rid or "")
        if card is None or (not card.done and card.result is None):
            self.notify("nothing to save yet")
            return
        ensure_dirs()
        path = os.path.join(EXPORTS_DIR, f"{card.rid}.md")
        try:
            with open(path, "w") as f:
                f.write(f"# {card.task_text}\n\n{card.detail_text()}\n")
        except OSError:
            self.notify("could not save")
            return
        self.notify(f"saved: {path}")

    def _notify_done(self, res: dict) -> None:
        try:
            msg = (f"{res.get('task') or 'task'}: "
                   f"{res.get('summary') or res.get('error') or 'done'}")
            msg = msg.replace("\\", "\\\\").replace('"', '\\"')[:200]
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{msg}" with title "muse-cli"'],
                timeout=5, capture_output=True)
        except Exception:
            pass

    def action_scroll_bottom(self) -> None:
        self.task_list.scroll_end(animate=False)

    def _refresh_statusbar(self, extra: str = "") -> None:
        t = Text()
        t.append("muse-cli · ")
        t.append(self.session_id, style="bold")
        t.append(" · cwd: ")
        t.append(self.session_cwd)
        t.append(" · bridge ")
        t.append("●", style="green")
        t.append(" · muse ")
        if self._muse_running:
            t.append("● working", style="green")
        else:
            t.append("○ idle", style="dim")
        active, waiting = self._task_counts()
        if active:
            t.append(f" · {active} active", style="cyan")
        if waiting:
            t.append(f" · {waiting} awaiting approval", style="yellow")
        if os.path.exists(PAUSED_PATH):
            t.append(" · ⏸ paused", style="yellow")
        if self.settings.get("auto_approve"):
            t.append(" · ⚡ auto-approve ON", style="yellow")
        if extra:
            t.append(f" · {extra}")
        self.statusbar.update(t)
