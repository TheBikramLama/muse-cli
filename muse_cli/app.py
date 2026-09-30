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
import selectors
import subprocess
import threading
import time
import uuid

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.command import CommandPalette, Hit, Hits, Provider
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import Screen
from textual.widgets import (Footer, Input, Label, ListItem, ListView,
                              Markdown, Static, TextArea)

from .bridge import CLAIM_SUFFIX, Bridge
from .config import load_settings, save_settings
from . import pairing
from .instance import pid_alive
from .pairing_screen import PairingScreen
from .paths import (APPROVAL_DIR, DEFAULT_SESSION, EXPORTS_DIR, INPUT_HISTORY_PATH,
                   MESSAGES_DIR, PAUSED_PATH, QUEUE_DIR, REPLIES_DIR, SCRIPTS_DIR,
                   SEEN_PATH, SESSIONS_DIR, SETTINGS_PATH, STATUS_DIR,
                   TODOS_DIR, TUI_CMD_DIR, WATCHER_JSON, WATCHER_STALE_S,
                   ensure_dirs, valid_session)
from .protocol import cancel, new_id, submit
from .runner import check_cwd
from .sessions import load_recent, log_task, new_session
from .skills import install_skill, list_skills, read_skill, remove_skill

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
CHUNK_THROTTLE_S = 0.15

# -- input completion (zsh-style suggestions for / and !) -------------------
SLASH_COMMANDS = [
    "/autoapprove", "/cd", "/clear", "/export", "/heartbeat", "/help",
    "/quit", "/restart", "/run", "/scripts", "/session", "/sessions",
    "/settings", "/sidebar", "/skill", "/skills", "/todo", "/todos",
]
SKILL_SUBCOMMANDS = ["install", "remove", "show"]
COMP_MAX = 10


def _path_candidates(prefix: str, cwd: str) -> list[str]:
    """Complete prefix as a filesystem path. Directories get a trailing /."""
    exp = os.path.expanduser(prefix)
    if not prefix or prefix.endswith("/") or prefix == "~":
        dir_exp, base, head = exp or cwd, "", prefix
    else:
        dir_exp, base = os.path.split(exp)
        if not dir_exp:
            dir_exp = cwd
        head = prefix[:len(prefix) - len(base)] if base else prefix
    try:
        entries = sorted(os.listdir(dir_exp or "."))
    except OSError:
        return []
    out: list[str] = []
    for e in entries:
        if not e.startswith(base):
            continue
        if e.startswith(".") and not base.startswith("."):
            continue
        full = os.path.join(dir_exp, e)
        suffix = "/" if os.path.isdir(full) else ""
        out.append(head + e + suffix)
        if len(out) >= 20:
            break
    return out


def _exe_candidates(prefix: str) -> list[str]:
    """Complete prefix as a command name from PATH (first ! word)."""
    if "/" in prefix:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for e in entries:
            if e.startswith(prefix) and e not in seen:
                p = os.path.join(d, e)
                if os.path.isfile(p) and os.access(p, os.X_OK):
                    seen.add(e)
                    out.append(e)
        if len(out) >= 40:
            break
    return sorted(out)


def complete_token(text: str, cursor: int, history: list[str], cwd: str,
                   scripts: list[str], sessions: list[str],
                   todos: list[str], skills: list[str]
                   ) -> tuple[int, int, list[str]]:
    """Suggest completions for the token around the cursor.

    Returns (start, end, candidates) where [start:end] is the token to
    replace. Only / commands and ! shell get suggestions; plain messages
    (chat text to Muse) return no candidates.
    """
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = cursor
    while end < len(text) and not text[end].isspace():
        end += 1
    token = text[start:cursor]

    def _hist(prefix: str) -> list[str]:
        out: list[str] = []
        for h in reversed(history):
            if h.startswith(prefix) and h != text[:cursor] and h not in out:
                out.append(h)
            if len(out) >= 8:
                break
        return out

    if text.startswith("/"):
        head = text[:start]
        parts = head.split()
        if len(parts) <= 1:
            # completing the command itself (token includes the leading /)
            cands = [c for c in SLASH_COMMANDS if c.startswith(token)]
            return start, end, (cands + _hist(text[:cursor]))[:COMP_MAX]
        name = parts[0][1:].lower()
        arg_i = len(parts) - 1
        if name == "run":
            cands = [s for s in scripts if s.startswith(token)]
        elif name == "cd":
            cands = _path_candidates(token, cwd)
        elif name == "todo":
            names = ["clear"] + [(t[:-3] if t.endswith(".md") else t)
                                 for t in todos]
            cands = [t for t in names if t.startswith(token)]
        elif name == "session":
            cands = [s for s in sessions if s.startswith(token)]
        elif name == "skill" and arg_i == 1:
            cands = [s for s in SKILL_SUBCOMMANDS if s.startswith(token)]
        elif name == "skill" and arg_i == 2 and parts[1] in ("remove", "show"):
            cands = [s for s in skills if s.startswith(token)]
        else:
            cands = []
        return start, end, cands[:COMP_MAX]
    if text.startswith("!"):
        if start == 0:
            # first word: the token includes the leading "!" — strip it
            # before matching PATH commands and history lines
            word = token[1:]
            exes = ["!" + e for e in _exe_candidates(word)]
            h = _hist("!" + word)
            merged = h + [e for e in exes if e not in h]
            return start, end, merged[:COMP_MAX]
        return start, end, _path_candidates(token, cwd)[:COMP_MAX]
    return start, end, []

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
  /todos         show your todo lists in the sidebar
  /todo <name>   expand a todo list in the sidebar
  /todo clear [name]  delete one todo list, or all of them
  /sidebar       toggle the todo sidebar (works on narrow terminals too)
  /heartbeat <m> ping Muse for updates every m minutes (/heartbeat off)
  /autoapprove [on|off]  toggle auto-approval of approval requests
  /settings      show where settings.json lives
  /sessions      list past sessions (most recent first)
  /session <name> switch the history log new tasks are logged to
                 (the history bucket — not the instance. Parallel
                 windows use ./run.sh --session <name>.)
  /export [name]  save this session's finished tasks as markdown
  /clear         clear the screen and wipe its history
  /restart       restart the TUI (picks up new code)
  /help          this help
  /quit          exit

Aliases: /exit = /quit, /q = /quit, /h = /help, /resume = /sessions

Keys (when the input box is not focused — press Esc to leave it):
  j/k move selection between tasks
  esc esc  stop everything running (failsafe)
  /   jump back to the input box
  c   show/hide the selected task's commands + full output
  x   cancel the selected (or currently running) task
  p   pause/resume the bridge queue
  r   retry the selected finished task
  a   approve the selected task (when awaiting approval)
  d   deny the selected task (when awaiting approval)
  A   toggle auto-approve for approval requests
  shift+tab  toggle Approval mode / Auto mode
  t   show/hide the todo sidebar
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
    """One unit of work.

    Compact by design: the card shows a header (status icon, title,
    elapsed) plus a single live/summary line. Commands and full output
    stay hidden behind `c` (toggle_detail). Finished summaries carry
    timestamps so the feed reads as a quiet log.
    """

    def __init__(self, rid: str, task: str, source: str,
                 cmd: list, cwd: str | None,
                 restore_rec: dict | None = None,
                 steps: list | None = None,
                 skills: list | None = None) -> None:
        super().__init__(classes="task-card")
        self.rid = rid
        self.task_text = task or "(no description)"
        self.source = source
        self.cmd = cmd or []
        self.cwd = cwd or "~"
        self.steps = steps or []
        self.skills = [str(s) for s in (skills or [])]
        self._step_text = ""
        self.t0 = time.time()
        self.t0_str = time.strftime("%H:%M:%S", time.localtime(self.t0))
        self.done = False
        self.result: dict | None = None
        self._line = ""  # single live line (last chunk or step), not a tail
        self._status_mtime = 0.0  # last seen mtime of STATUS_DIR/<rid>.txt
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
            if self.skills:
                yield Static("⚙ " + ",".join(self.skills), classes="task-src")
            self.elapsed = Static("", classes="task-elapsed")
            yield self.elapsed
        self.live = Static("queued…", classes="live-line")
        yield self.live

    def on_click(self, event: events.Click) -> None:
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
        self._line = ""
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
        self.live.update(self._step_text or self._line or "\u2026")

    def push_chunk(self, line: str) -> None:
        if not self._composed:
            return
        line = line.rstrip()
        if not line:
            return
        self._line = line  # compact: only the latest line is shown
        self._render_live()

    def tick(self, frame: int) -> None:
        if self.done or not self._composed:
            return
        self.icon.update(SPINNER[frame % len(SPINNER)])
        self.elapsed.update(f"{time.time() - self.t0:.0f}s")
        self._poll_agent_status()

    def _poll_agent_status(self) -> None:
        """Pick up agent-pushed realtime status.

        While a task runs, the Muse-side agent may write human-readable
        progress lines to ``~/.muse/status/<rid>.txt`` (one per line). The
        latest non-empty line becomes the card's live line, so the user sees
        what the agent is actually doing instead of a stale spinner text.
        """
        try:
            sp = os.path.join(STATUS_DIR, self.rid + ".txt")
            mt = os.path.getmtime(sp)
        except OSError:
            return
        if mt <= self._status_mtime:
            return
        self._status_mtime = mt
        try:
            with open(sp, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
        except OSError:
            return
        if lines:
            self._line = lines[-1]
            self._step_text = ""
            self._render_live()

    def finish(self, res: dict) -> None:
        if not self._composed:
            # Mount not processed yet (instant task); on_mount applies it.
            self._pending_result = res
            return
        self.done = True
        self.awaiting = False
        self.result = res
        try:
            os.remove(os.path.join(STATUS_DIR, self.rid + ".txt"))
        except OSError:
            pass
        ok = bool(res.get("ok")) and res.get("exit", 1) == 0
        self.icon.update("✓" if ok else "✗")
        self.icon.remove_class("running")
        self.icon.add_class("done" if ok else "failed")
        dur = res.get("duration_s", 0) or 0
        self.elapsed.update(f"{dur:.1f}s")
        ended = res.get("ended_at")
        ts = (time.strftime("%H:%M:%S", time.localtime(ended))
              if isinstance(ended, (int, float)) else
              time.strftime("%H:%M:%S", time.localtime()))
        summary = res.get("summary") or res.get("error") or "done"
        self.live.update(f"{summary} · {ts} · {dur:.1f}s")
        if self._detail is None:
            self._detail = TextArea(self._detail_text(res), read_only=True,
                                    classes="detail")
            self._detail.display = False
            self.mount(self._detail)
        else:
            # Detail view was lazily built while running; refresh with result.
            try:
                self._detail.text = self._detail_text(res)
            except Exception:
                pass

    def restore(self, rec: dict) -> None:
        """Rebuild a finished card from session history."""
        self.done = True
        ok = bool(rec.get("ok")) and rec.get("exit", 1) == 0
        self.icon.update("✓" if ok else "✗")
        self.icon.remove_class("running")
        self.icon.add_class("done" if ok else "failed")
        dur = rec.get("duration_s", 0) or 0
        self.elapsed.update(f"{dur:.1f}s")
        summary = rec.get("summary") or ""
        self.live.update(f"{summary} · {dur:.1f}s · previous session"
                         if summary else f"{dur:.1f}s · previous session")
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
        started = res.get("started_at")
        ended = res.get("ended_at")
        when = ""
        if isinstance(started, (int, float)):
            when = time.strftime("%H:%M:%S", time.localtime(started))
            if isinstance(ended, (int, float)):
                when += " → " + time.strftime("%H:%M:%S", time.localtime(ended))
        elif hasattr(self, "t0_str"):
            when = self.t0_str
        lines = [
            first,
            f"in {self.cwd} · {self.source} · {res.get('duration_s', 0):.1f}s"
            + (f" · {when}" if when else ""),
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
            # Lazily build the detail view so `c` works while the task is
            # still running (shows the command + what we know so far).
            try:
                self._detail = TextArea(
                    self._detail_text(self.result or {}), read_only=True,
                    classes="detail")
                self._detail.display = False
                self.mount(self._detail)
            except Exception:
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
    """A chat message — yours (outgoing) or Muse's reply (incoming).

    Outgoing cards carry a live status: "sent · waiting for Muse" →
    "Muse is writing…" → "replied ✓", driven by the watcher heartbeat
    (~/.muse/watcher.json) and the reply file.
    """

    def __init__(self, mid: str, text: str, incoming: bool,
                 at: float | None = None) -> None:
        super().__init__(classes="msg-card" + (" incoming" if incoming else ""))
        self.mid = mid
        self.incoming = incoming
        self.at = at or time.time()
        self._text = text
        self._status_text = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task-head"):
            yield Static("💬" if self.incoming else "🗨", classes="ticon done")
            who = "Muse" if self.incoming else "you"
            yield Static(who, classes="task-title")
            if not self.incoming:
                self.status = Static(self._status_text, classes="task-src")
                yield self.status
            yield Static(time.strftime("%H:%M", time.localtime(self.at)),
                         classes="task-src")
        yield Markdown(self._text, classes="msg-body")

    def set_status(self, text: str) -> None:
        """Update the outgoing status line (no-op for incoming cards)."""
        if self.incoming or text == self._status_text:
            return
        self._status_text = text
        try:
            self.status.update(text)
        except AttributeError:
            pass  # not composed yet; compose() picks up _status_text


def _parse_todo(text: str) -> tuple[int, int, list[tuple[bool, str]]]:
    """Parse a markdown checklist.

    Returns (done, total, items) where items is a list of
    (checked, label) for every "- [ ]"/"- [x]" line. The file stays the
    source of truth; this is just the rendering model for the sidebar.
    """
    items: list[tuple[bool, str]] = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s+\[( |x|X)\]\s*(.*)$", line)
        if m:
            items.append((m.group(1).lower() == "x", m.group(2).strip()))
    done = sum(1 for c, _ in items if c)
    return done, len(items), items


class CmdInput(Input):
    """The command box, with completion-dropdown key handling.

    Tab/Shift+Tab/Up/Down/Enter/Escape are intercepted here (before the
    Screen's focus bindings) when the completion dropdown is open; Tab
    also triggers completion. Everything else bubbles to the app as before.
    """

    def on_key(self, event: events.Key) -> None:
        app = self.app
        if not isinstance(app, MuseCliApp):
            return
        key = event.key
        if key == "tab":
            if app.completion_accept_or_open():
                event.prevent_default()
                event.stop()
        elif key == "shift+tab":
            app.action_toggle_auto_approve()
            event.prevent_default()
            event.stop()
        elif app.completion_open:
            if key == "escape":
                app.completion_close()
                # Count the press toward double-Esc so the failsafe needs
                # exactly two presses even with the dropdown open.
                app._esc_tap(blur=False)
            elif key == "up":
                app.completion_move(-1)
            elif key == "down":
                app.completion_move(1)
            elif key == "enter":
                # A fully typed, valid /command submits on Enter — the
                # auto-opened dropdown must not rewrite it (e.g. into a
                # history entry). Partial tokens still accept via Enter.
                if not app.completion_enter_accepts():
                    return
                app.completion_accept()
            else:
                return
            event.prevent_default()
            event.stop()


class MuseScreen(Screen):
    """Default screen without tab/shift+tab focus bindings.

    Tab is the completion key and Shift+Tab toggles the approval mode;
    both are handled by the app instead of moving focus.
    """
    BINDINGS = [b for b in Screen.BINDINGS
                if b.key not in ("tab", "shift+tab")]


class TodoHead(Static):
    """One clickable todo-list header in the sidebar.

    Clicking expands this list (collapsing the others — one at a time).
    """

    def __init__(self, name: str) -> None:
        super().__init__("", classes="todo-head")
        self.tname = name

    def on_click(self, event: events.Click) -> None:
        app = self.app
        if isinstance(app, MuseCliApp):
            app.expand_todo(self.tname)


class MuseCommands(Provider):
    """Our app actions in the ^p command palette.

    Textual's palette only knows its own system commands (Quit, Theme,
    Screenshot, ...). This provider adds the actions from our key bar so
    they're searchable and runnable by keyboard.
    """

    COMMANDS: list[tuple[str, str, str]] = [
        # (action name, title, help text)
        ("toggle_sidebar", "Toggle todo sidebar",
         "show or hide the todo sidebar"),
        ("toggle_pause", "Pause / resume queue",
         "hold the task queue or let it run"),
        ("toggle_auto_approve", "Toggle auto-approve mode",
         "approval requests run immediately, or park for a/d"),
        ("approve_task", "Approve selected task",
         "approve the selected approval request"),
        ("deny_task", "Deny selected task",
         "deny the selected approval request"),
        ("retry_task", "Retry selected task",
         "re-run the selected task"),
        ("cancel_task", "Cancel selected task",
         "cancel the selected task"),
        ("save_output", "Save selected task output",
         "save the selected task's output to exports/"),
        ("copy_task", "Copy selected task detail",
         "copy the selected task's detail to the clipboard"),
        ("toggle_detail", "Toggle task detail",
         "show or hide a task's commands and full output"),
        ("scroll_bottom", "Scroll to bottom",
         "jump the task list to the newest card"),
    ]

    async def search(self, query: str) -> Hits:
        app = self.screen.app
        matcher = self.matcher(query)
        for action, title, help_text in self.COMMANDS:
            if (score := matcher.match(title)) > 0:
                callback = getattr(app, "action_" + action, None)
                if callable(callback):
                    yield Hit(score, matcher.highlight(title), callback,
                              help=help_text)


class MuseCliApp(App):
    TITLE = "muse-cli"
    CSS = """
    #topline {
        height: 1; min-height: 1;
        background: $surface; color: $text;
        padding: 0 1;
    }
    #modeline {
        height: 1;
        background: $surface; color: $text-muted;
        padding: 0 1;
    }
    #main { height: 1fr; }
    #tasks { width: 1fr; height: 1fr; }
    #todoside {
        width: 34; height: 1fr;
        border-left: solid $primary-darken-2;
        background: $surface; padding: 0 1;
    }
    #todoside.hidden { display: none; }
    .todo-side-head { text-style: bold; color: $text-muted; margin-bottom: 1; }
    .todo-head { height: 1; color: $text; text-style: bold; }
    .todo-head:hover { background: $surface-lighten-1; }
    .todo-item { height: auto; color: $text; padding-left: 2; }
    .todo-item.done { color: $text-muted; }
    .todo-sec { height: auto; margin-bottom: 1; }
    /* Command palette: keep it off the screen edges with a visible frame. */
    CommandPalette > Vertical {
        margin: 2 8;
        height: 1fr;
        border: solid $primary-darken-2;
    }
    #activitybar {
        height: 1; padding: 0 1;
        background: $surface-darken-1;
    }
    #activity-left { width: 1fr; }
    #activity-right { width: auto; color: $text-muted; }
    #empty {
        text-align: center;
        color: $text-muted;
        padding: 2 1;
    }
    #cmd { margin: 1 1 0 1; padding: 0 1; }
    #completion {
        height: auto; max-height: 8; margin: 0 1;
        border: solid $primary-darken-2;
        background: $surface; display: none;
    }
    #completion.open { display: block; }
    #completion > ListItem { padding: 0 1; }
    #cwdline {
        height: 1; padding: 0 1; color: $text-muted;
    }
    #toasts {
        layer: toasts; dock: top;
        width: 1fr; height: auto; align: right top;
    }
    .toast-holder { width: 1fr; height: auto; align-horizontal: right; }
    .toast {
        width: 48; max-width: 60%; height: auto;
        margin: 1 1 0 0; padding: 0 1;
        background: $panel-lighten-1;
        border-left: outer $success;
    }
    .toast.-warning { border-left: outer $warning; }
    .toast.-error { border-left: outer $error; }
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
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "toggle_detail", "Commands"),
        ("x", "cancel_task", "Cancel"),
        ("a", "approve_task", "Approve"),
        ("d", "deny_task", "Deny"),
        ("shift+tab", "toggle_auto_approve", "Mode"),
        ("p", "toggle_pause", "Pause"),
        ("t", "toggle_sidebar", "Todos"),
        ("r", "retry_task", "Retry"),
        ("s", "save_output", "Save"),
        ("y", "copy_task", "Copy"),
        ("g", "scroll_bottom", "Bottom"),
    ]
    # ^p command palette: Textual's system commands plus our own provider.
    COMMANDS = App.COMMANDS | {MuseCommands}
    # Below this terminal width the todo sidebar auto-hides — on a narrow
    # screen it eats too much of the chat. The ☑ n/m aggregate stays.
    SIDEBAR_MIN_WIDTH = 100

    def __init__(self, session: str = DEFAULT_SESSION) -> None:
        super().__init__()
        self.settings = load_settings()
        # Instance session (--session): this window only runs queue items,
        # messages and replies tagged with it. Parallel windows use
        # different sessions. Distinct from session_id below, which is just
        # the history-log bucket new tasks are written to (/session).
        self.instance_session = session if valid_session(session) else DEFAULT_SESSION
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
        self._todo_data: dict[str, dict] = {}
        self._todo_expanded: str | None = None
        self._sidebar_key: object = None
        # Watcher visibility: last ~/.muse/watcher.json payload (None = never
        # seen) plus outgoing message cards by mid for status updates.
        self._watcher: dict | None = None
        self._watcher_mtime = 0.0
        self._msg_cards: dict[str, MessageCard] = {}
        # Completion dropdown state.
        self.completion_open = False
        self._comp_items: list[str] = []
        self._comp_idx = 0
        self._comp_start = 0
        self._comp_end = 0
        # Todo sidebar: shown when a todo list has open items; `t` toggles
        # a manual override (hide, or show even on a narrow terminal).
        self._sidebar_manual_hide = False
        self._sidebar_manual_show = False
        # Heartbeat: periodic ping to Muse for updates. Timer handle,
        # last tick time, restored from settings on mount.
        self._heartbeat_timer = None
        self._heartbeat_last = 0.0
        # Double-Esc failsafe: two presses within this window stop everything.
        self._last_esc = 0.0

    def get_default_screen(self) -> Screen:
        return MuseScreen()

    # -- top-right toasts (override: never the bottom-right rack) --
    def notify(self, message: object, *, title: str = "",
               severity: str = "information", timeout: float = 4) -> None:
        """Show a toast top-right. Thread-safe; replaces App.notify."""
        try:
            self.call_from_thread(self._show_toast, str(message), title,
                                  severity, timeout)
        except RuntimeError:
            if self.is_running:
                try:
                    self._show_toast(str(message), title, severity, timeout)
                except Exception:
                    pass

    def _show_toast(self, message: str, title: str, severity: str,
                    timeout: float) -> None:
        try:
            holder = self.query_one("#toasts", Vertical)
        except Exception:
            return
        kids = list(holder.children)
        if len(kids) >= 4:
            try:
                kids[0].remove()
            except Exception:
                pass
        text = Text()
        if title:
            text.append(title + "\n", style="bold")
        text.append(message)
        toast = Static(text, classes=f"toast -{severity}")
        holder.mount(toast)
        self.set_timer(timeout, toast.remove)

    def compose(self) -> ComposeResult:
        self.topline = Static("", id="topline")
        yield self.topline
        self.toasts = Vertical(id="toasts")
        yield self.toasts
        with Horizontal(id="main"):
            self.task_list = ScrollableContainer(id="tasks")
            yield self.task_list
            self.todoside = Vertical(id="todoside")
            yield self.todoside
        with Horizontal(id="activitybar"):
            self.activity_left = Static("", id="activity-left")
            yield self.activity_left
            self.activity_right = Static("", id="activity-right")
            yield self.activity_right
        self.completion = ListView(id="completion")
        yield self.completion
        self.cmd_input = CmdInput(
            placeholder=("/ command · ! shell · text = message to Muse"
                         " · tab completes · shift+tab switches mode"),
            id="cmd")
        yield self.cmd_input
        self.cwdline = Static("", id="cwdline")
        yield self.cwdline
        self.modeline = Static("", id="modeline")
        yield self.modeline
        yield Footer()

    def on_mount(self) -> None:
        # First run (or after `muse-cli unpair`): connect to the Muse app
        # before anything else. The screen dismisses itself once the
        # handshake validates.
        if not pairing.is_paired():
            self.push_screen(PairingScreen())
        self._refresh_statusbar()
        # Restore heartbeat timer if it was enabled in settings.
        hb = self.settings.get("heartbeat_minutes", 0)
        if hb:
            try:
                self._heartbeat_timer = self.set_interval(
                    int(hb) * 60, self._heartbeat_tick)
            except Exception:
                pass
        self.empty_state = Static(
            "no tasks yet — /help for commands · ! for shell · plain text messages Muse",
            id="empty")
        self.task_list.mount(self.empty_state)
        self.todoside.mount(Static("☑ todo lists", classes="todo-side-head"))
        self._refresh_sidebar()
        for rec in load_recent(self.settings.get("tui", {}).get("history_limit", 50)):
            card = TaskCard(rec.get("id", "?"), rec.get("task", ""),
                            rec.get("source", "muse"), rec.get("cmd", []),
                            rec.get("cwd"), restore_rec=rec,
                            skills=rec.get("skills"))
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
            session=self.instance_session,
        )
        self._bridge.start()
        for req in self._bridge.pending_approvals():
            self._on_approval(req)
        self._refresh_statusbar()
        self.set_interval(0.1, self._tick)
        self.task_list.scroll_end(animate=False)
        try:
            self.cmd_input.focus()
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
                            steps=req.get("steps"), skills=req.get("skills"))
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
                            steps=req.get("steps"), skills=req.get("skills"))
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
        show_empty = not self.cards and not self._todo_data
        if show_empty != self._empty_shown:
            self._empty_shown = show_empty
            self.empty_state.display = show_empty
        active, _ = self._task_counts()
        if active != self._last_active:
            self._last_active = active
            self._refresh_statusbar()
        self._poll_n += 1
        self._refresh_activity()
        if self._poll_n % 10 == 0:  # ~1s
            self._poll_replies()
            self._poll_todos()
            self._poll_watcher()
            self._poll_tui_cmd()
        if self._poll_n % 100 == 0:  # ~10s: staleness is time-based, so the
            self._refresh_statusbar()  # watcher segment needs a periodic nudge

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
            # Replies are routed per session; the watcher echoes the
            # message's session tag. Missing tag = "main" (pre-session).
            if payload.get("session", DEFAULT_SESSION) != self.instance_session:
                continue
            card = MessageCard(mid, text, incoming=True,
                               at=payload.get("at") or time.time())
            self.task_list.mount(card)
            self.task_list.scroll_end(animate=False)
            self._seen_replies.add(mid)
            out = self._msg_cards.get(mid)
            if out is not None:
                out.set_status("replied ✓")
            new = True
            self.notify("💬 reply from Muse")
        if new:
            self._save_seen()

    # -- watcher visibility: ~/.muse/watcher.json heartbeat --
    def _poll_watcher(self) -> None:
        """Read the inbox-watcher's heartbeat; staleness itself is the
        down signal (a dead watcher can't write anything)."""
        try:
            mtime = os.path.getmtime(WATCHER_JSON)
        except OSError:
            if self._watcher is not None or self._watcher_mtime:
                self._watcher, self._watcher_mtime = None, 0.0
                self._refresh_statusbar()
                self._refresh_msg_states()
            return
        if mtime != self._watcher_mtime:
            self._watcher_mtime = mtime
            try:
                with open(WATCHER_JSON) as f:
                    payload = json.load(f)
                self._watcher = payload if isinstance(payload, dict) else None
            except (OSError, ValueError):
                self._watcher = None
            self._refresh_statusbar()
        self._refresh_msg_states()

    def _watcher_segment(self) -> tuple[str, str]:
        """(text, style) for the status bar's watcher health indicator."""
        w = self._watcher
        if w is None:
            return "○ not seen", "dim"
        if not w.get("ok", True):
            return "⚠ error", "red"
        try:
            age = time.time() - float(w.get("at") or 0)
        except (TypeError, ValueError):
            age = float("inf")
        if age > WATCHER_STALE_S:
            mins = int(age // 60)
            return f"⚠ silent {mins}m", "yellow"
        if w.get("state") == "writing":
            return "✎ writing…", "cyan"
        return "●", "green"

    def _msg_status(self, mid: str) -> str:
        if os.path.exists(os.path.join(REPLIES_DIR, mid + ".json")):
            return "replied ✓"
        w = self._watcher
        if (isinstance(w, dict) and w.get("state") == "writing"
                and w.get("mid") == mid):
            return "Muse is writing…"
        return "sent · waiting for Muse"

    def _refresh_msg_states(self) -> None:
        for mid, card in self._msg_cards.items():
            card.set_status(self._msg_status(mid))

    # -- todos: ~/.muse/todos/*.md watched live --
    def _poll_todos(self) -> None:
        # Todo lists live ONLY in the sidebar now — no chat cards.
        # Files under ~/.muse/todos/*.md are the source of truth.
        ensure_dirs()
        try:
            names = sorted(f for f in os.listdir(TODOS_DIR)
                           if f.endswith(".md"))
        except OSError:
            names = []
        seen = set()
        for name in names:
            p = os.path.join(TODOS_DIR, name)
            try:
                mtime = os.path.getmtime(p)
            except OSError:
                continue
            rec = self._todo_data.get(name)
            if rec is None or rec.get("mtime") != mtime:
                try:
                    with open(p) as f:
                        text = f.read()
                except OSError:
                    continue
                done, total, items = _parse_todo(text)
                self._todo_data[name] = {"mtime": mtime, "done": done,
                                         "total": total, "items": items}
            seen.add(name)
        for name in list(self._todo_data):
            if name not in seen:
                del self._todo_data[name]
        if self._todo_expanded not in self._todo_data:
            self._todo_expanded = None
            for name in sorted(self._todo_data):
                if self._todo_data[name]["done"] < self._todo_data[name]["total"]:
                    self._todo_expanded = name
                    break
            if self._todo_expanded is None and self._todo_data:
                self._todo_expanded = sorted(self._todo_data)[0]
        self._refresh_sidebar()

    # -- remote control: ~/.muse/tui-cmd/*.json ------------------------------
    # Any Muse chat (or script) can drive this TUI window by dropping a
    # JSON file here. Files are consumed and deleted. The "session" field
    # routes the op to the right TUI window; other windows ignore it.
    # Ops: notify{ text }, card{ text }, run{ task, cmd[], cwd?, timeout? },
    # clear{}, mode{ "auto" | "approval" }, restart{}.
    def _poll_tui_cmd(self) -> None:
        try:
            names = sorted(os.listdir(TUI_CMD_DIR))
        except OSError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            p = os.path.join(TUI_CMD_DIR, name)
            try:
                with open(p) as f:
                    payload = json.load(f)
            except (OSError, ValueError):
                payload = None
            if not isinstance(payload, dict):
                # Unreadable junk: remove so a bad file can't wedge the loop.
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
            if (payload.get("session") or DEFAULT_SESSION) != self.instance_session:
                # Another window's command — leave it for that window.
                continue
            try:
                os.remove(p)
            except OSError:
                pass
            self._handle_tui_cmd(payload)

    def _handle_tui_cmd(self, p: dict) -> None:
        if (p.get("session") or DEFAULT_SESSION) != self.instance_session:
            return  # another window's business
        op = p.get("op")
        if op == "notify":
            self.notify(str(p.get("text", ""))[:300])
        elif op == "card":
            text = p.get("text")
            if isinstance(text, str) and text.strip():
                card = MessageCard(p.get("id") or new_id(), text,
                                   incoming=True, at=time.time())
                self.task_list.mount(card)
                self.task_list.scroll_end(animate=False)
                self.notify("💬 card from Muse")
        elif op == "run":
            cmd = p.get("cmd")
            if isinstance(cmd, list) and cmd:
                task = str(p.get("task") or " ".join(cmd)[:80])
                rid = submit(task=task, cmd=cmd,
                             cwd=p.get("cwd") or self.session_cwd,
                             timeout=p.get("timeout") or 120,
                             source="muse", session=self.instance_session)
                card = TaskCard(rid, task, "muse", cmd, self.session_cwd)
                self.cards[rid] = card
                self.focused_rid = rid
                self.task_list.mount(card)
                self.task_list.scroll_end(animate=False)
                self.refresh_selection()
        elif op == "clear":
            for rid, card in list(self.cards.items()):
                if card.done:
                    card.remove()
                    del self.cards[rid]
            self.focused_rid = None
            self.refresh_selection()
        elif op == "mode":
            mode = p.get("mode")
            if mode == "auto":
                self._set_auto_approve(True)
            elif mode == "approval":
                self._set_auto_approve(False)
        elif op == "restart":
            self._restart_self()

    # -- todo sidebar -------------------------------------------------------
    def _todo_counts(self) -> tuple[int, int]:
        """(done, total) summed across todo lists."""
        done = sum(r["done"] for r in self._todo_data.values())
        total = sum(r["total"] for r in self._todo_data.values())
        return done, total

    def _has_active_todos(self) -> bool:
        done, total = self._todo_counts()
        return total > 0 and done < total

    def _refresh_sidebar(self) -> None:
        try:
            side = self.todoside
        except AttributeError:
            return
        # The 1s poll calls this constantly; only rebuild the DOM when the
        # underlying data changed, so clicks land on stable widgets.
        key = (tuple(sorted((n, r["mtime"]) for n, r in self._todo_data.items())),
               self._todo_expanded)
        if key != self._sidebar_key:
            self._sidebar_key = key
            for sec in list(side.query(".todo-sec")):
                try:
                    sec.remove()
                except Exception:
                    pass
            for name in sorted(self._todo_data):
                rec = self._todo_data[name]
                expanded = name == self._todo_expanded
                sec = Vertical(classes="todo-sec")
                side.mount(sec)
                head = TodoHead(name)
                head.update(self._todo_head_text(
                    name, rec["done"], rec["total"], expanded))
                sec.mount(head)
                if expanded:
                    for checked, label in rec["items"]:
                        mark = "☑" if checked else "☐"
                        sec.mount(Static(
                            f"{mark} {label}",
                            classes="todo-item done" if checked else "todo-item"))
                    if not rec["items"]:
                        sec.mount(Static("(empty)", classes="todo-item done"))
        visible = (self._has_active_todos()
                   and not self._sidebar_manual_hide
                   and (self._sidebar_fits()
                        or self._sidebar_manual_show))
        try:
            side.set_class(not visible, "hidden")
        except Exception:
            pass
        self._refresh_activity()

    def _sidebar_fits(self) -> bool:
        """Whether the terminal is wide enough for the todo sidebar."""
        try:
            return self.size.width >= self.SIDEBAR_MIN_WIDTH
        except Exception:
            return True

    def on_resize(self, event: events.ResizeEvent) -> None:
        # Re-evaluate the narrow-screen sidebar auto-hide.
        self._refresh_sidebar()

    def _todo_head_text(self, name: str, done: int, total: int,
                        expanded: bool) -> Text:
        t = Text()
        short = name[:-3] if name.endswith(".md") else name
        t.append("▾ " if expanded else "▸ ", style="dim")
        t.append(short[:24])
        t.append(f"  {done}/{total}", style="dim")
        return t

    def expand_todo(self, name: str) -> None:
        """Expand one todo list in the sidebar, collapsing the others."""
        if name in self._todo_data:
            self._todo_expanded = name
            self._refresh_sidebar()

    def _clear_todos(self, name: str) -> None:
        """Delete todo list file(s): `/todo clear <name>` removes one list,
        bare `/todo clear` removes them all. Reads the directory directly
        (not the poll cache) so just-written lists are covered too."""
        try:
            files = [f for f in os.listdir(TODOS_DIR) if f.endswith(".md")]
        except OSError:
            files = []
        if name:
            key = name if name.endswith(".md") else name + ".md"
            if key not in files:
                self.notify(f"no todo list: {name} (see /todos)")
                return
            targets = [key]
        else:
            targets = sorted(files)
        n = 0
        for key in targets:
            try:
                os.remove(os.path.join(TODOS_DIR, key))
                n += 1
            except OSError:
                pass
            self._todo_data.pop(key, None)
        if self._todo_expanded in targets:
            self._todo_expanded = None
        self._refresh_sidebar()
        self.notify(f"cleared {n} todo list(s)" if n else "no todo lists")

    def cycle_todo(self, delta: int) -> None:
        """Keyboard: [ / ] moves the expanded list."""
        names = sorted(self._todo_data)
        if not names:
            return
        try:
            i = names.index(self._todo_expanded or "")
        except ValueError:
            i = 0 if delta > 0 else -1
        self.expand_todo(names[(i + delta) % len(names)])

    def action_toggle_sidebar(self) -> None:
        # Manual toggle overrides the narrow-screen auto-hide: if the
        # sidebar is currently visible, hide it; otherwise show it even
        # on a narrow terminal.
        currently_visible = (self._has_active_todos()
                             and not self._sidebar_manual_hide
                             and (self._sidebar_fits()
                                  or self._sidebar_manual_show))
        if currently_visible:
            self._sidebar_manual_hide = True
            self._sidebar_manual_show = False
        else:
            self._sidebar_manual_hide = False
            self._sidebar_manual_show = True
        self._refresh_sidebar()

    def _toggle_todos(self) -> None:
        """Toggle the todo sidebar: hide if visible, show if hidden."""
        if not self._todo_data:
            self.notify("no todo lists — write markdown to ~/.muse/todos/")
            return
        # Reuse the manual toggle so narrow-screen override applies too.
        self.action_toggle_sidebar()
        if self._sidebar_manual_hide:
            self.notify("todo sidebar hidden")
        else:
            parts = [f"{n} · {r['done']}/{r['total']}"
                     for n, r in sorted(self._todo_data.items())]
            self.notify("todos (sidebar): " + "   ".join(parts))

    def _list_todos(self) -> None:
        if not self._todo_data:
            self.notify("no todo lists — write markdown to ~/.muse/todos/")
            return
        self._sidebar_manual_hide = False
        self._sidebar_manual_show = True
        self._refresh_sidebar()
        parts = [f"{n} · {r['done']}/{r['total']}"
                 for n, r in sorted(self._todo_data.items())]
        self.notify("todos (sidebar): " + "   ".join(parts))

    def _list_skills(self) -> None:
        skills = list_skills()
        if not skills:
            self.notify("no skills installed — /skill install <git-url|path>")
            return
        in_use: set[str] = set()
        for card in self.cards.values():
            if not card.done:
                in_use.update(card.skills)
        lines = ["skills:"]
        for s in skills:
            mark = "●" if s["name"] in in_use else "○"
            desc = f" — {s['description']}" if s["description"] else ""
            lines.append(f"  {mark} {s['name']}{desc}")
        lines.append("● in use · ○ installed")
        self.task_list.mount(Static("\n".join(lines), classes="help-card"))
        self.task_list.scroll_end(animate=False)

    def _skill_cmd(self, arg: str) -> None:
        parts = arg.split(None, 1)
        sub = parts[0] if parts else ""
        rest = parts[1] if len(parts) > 1 else ""
        if sub == "install":
            src = rest.strip()
            if not src:
                self.notify("usage: /skill install <git-url|path>")
                return
            self.notify(f"installing skill from {src}…")

            def _do() -> None:
                ok, msg = install_skill(src)
                try:
                    self.call_from_thread(self.notify,
                                          ("✓ " if ok else "✗ ") + msg)
                except RuntimeError:
                    pass

            threading.Thread(target=_do, daemon=True).start()
        elif sub == "remove":
            self.notify(remove_skill(rest)[1])
        elif sub == "show":
            name = rest.strip()
            content = read_skill(name)
            if content is None:
                self.notify(f"no such skill: {name or '?'} (see /skills)")
            else:
                self.task_list.mount(
                    Static(f"skill: {name}\n\n{content}", classes="help-card"))
                self.task_list.scroll_end(animate=False)
        else:
            self.notify("usage: /skill install <git-url|path> | "
                        "/skill remove <name> | /skill show <name>")

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
                        if p.get("session", DEFAULT_SESSION) != self.instance_session:
                            continue
                        items.append((p.get("at") or 0, incoming,
                                      name[:-5], t))
                except (OSError, ValueError):
                    continue
        items.sort(key=lambda x: x[0])
        for at, incoming, mid, text in items[-30:]:
            if incoming:
                self._seen_replies.add(mid)
            card = MessageCard(mid, text, incoming, at)
            if not incoming:
                self._msg_cards[mid] = card
                card.set_status(self._msg_status(mid))
            self.task_list.mount(card)
        if items:
            self._save_seen()

    def _wipe_feed_history(self) -> int:
        """Delete the persisted state behind the feed so /clear survives
        a restart: all session JSONL files (task history restores from
        every bucket) and this window's message/reply files (the startup
        filter only shows this instance session's). Returns the number
        of files removed."""
        wiped = 0
        try:
            names = os.listdir(SESSIONS_DIR)
        except OSError:
            names = []
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            try:
                os.remove(os.path.join(SESSIONS_DIR, name))
                wiped += 1
            except OSError:
                pass
        for d in (MESSAGES_DIR, REPLIES_DIR):
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                p = os.path.join(d, name)
                try:
                    with open(p) as f:
                        sess = json.load(f).get("session", DEFAULT_SESSION)
                except (OSError, ValueError):
                    continue
                if sess != self.instance_session:
                    continue
                try:
                    os.remove(p)
                    wiped += 1
                except OSError:
                    pass
        return wiped

    # -- input box --
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        self.completion_close()
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

    def on_input_changed(self, event: Input.Changed) -> None:
        # Refilter the open dropdown as the user types; auto-open it when
        # they start a /command or !shell so the list appears without Tab.
        if event.input.id != "cmd":
            return
        text = event.value or ""
        if not self.completion_open:
            if not (text.startswith("/") or text.startswith("!")):
                return
        start, end, cands = self._completion_data()
        token = event.value[start:event.input.cursor_position]
        if len(cands) == 1 and cands[0] == token:
            self.completion_close()
            return
        if not cands:
            self.completion_close()
            return
        self._comp_start, self._comp_end = start, end
        self._comp_items = cands
        self._comp_idx = 0
        self._render_completion()

    # -- completion ------------------------------------------------------
    def _completion_data(self) -> tuple[int, int, list[str]]:
        inp = self.cmd_input
        try:
            scripts = sorted(os.listdir(SCRIPTS_DIR))
        except OSError:
            scripts = []
        try:
            sessions = sorted(f[:-6] for f in os.listdir(SESSIONS_DIR)
                              if f.endswith(".jsonl"))
        except OSError:
            sessions = []
        todos = sorted(self._todo_data)
        try:
            skills = [s["name"] for s in list_skills()]
        except Exception:
            skills = []
        return complete_token(inp.value, inp.cursor_position, self._history,
                              self.session_cwd, scripts, sessions, todos,
                              skills)

    def _render_completion(self) -> None:
        lv = self.completion
        lv.clear()
        for c in self._comp_items:
            lv.append(ListItem(Label(c)))
        lv.index = self._comp_idx
        lv.add_class("open")
        self.completion_open = True

    def completion_close(self) -> None:
        if not self.completion_open:
            return
        self.completion_open = False
        self._comp_items = []
        try:
            self.completion.remove_class("open")
            self.completion.clear()
        except Exception:
            pass

    def completion_move(self, delta: int) -> None:
        if not self._comp_items:
            return
        self._comp_idx = (self._comp_idx + delta) % len(self._comp_items)
        try:
            self.completion.index = self._comp_idx
        except Exception:
            pass

    def completion_accept(self) -> bool:
        """Accept the highlighted candidate. False when nothing to accept."""
        if not self.completion_open or not self._comp_items:
            return False
        cand = self._comp_items[self._comp_idx]
        self._apply_completion(self._comp_start, self._comp_end, cand,
                               final=True)
        if cand.endswith("/"):
            # keep completing inside the directory
            self.completion_close()
            self.completion_accept_or_open()
        else:
            self.completion_close()
        return True

    def completion_enter_accepts(self) -> bool:
        """Whether Enter should accept the highlighted candidate.

        False when the box holds a complete, valid /command: the user
        typed something runnable, so Enter submits it instead of letting
        the auto-opened dropdown rewrite it into e.g. a history entry.
        """
        if not self.completion_open or not self._comp_items:
            return False
        text = (self.cmd_input.value or "").strip()
        if text.startswith("/"):
            words = text[1:].split()
            name = words[0].lower() if words else ""
            if "/" + name in SLASH_COMMANDS or name in SLASH_ALIASES:
                return False
        return True

    def completion_accept_or_open(self) -> bool:
        """Tab: accept the open highlight, or compute and show candidates.

        Returns True when the key was consumed.
        """
        if self.completion_open:
            return self.completion_accept()
        start, end, cands = self._completion_data()
        if not cands:
            return False
        if len(cands) == 1:
            self._apply_completion(start, end, cands[0], final=True)
            return True
        token = self.cmd_input.value[start:self.cmd_input.cursor_position]
        pref = os.path.commonprefix(cands)
        if len(pref) > len(token):
            self._apply_completion(start, end, pref)
            return True
        self._comp_start, self._comp_end = start, end
        self._comp_items = cands
        self._comp_idx = 0
        self._render_completion()
        return True

    def _apply_completion(self, start: int, end: int, cand: str,
                          final: bool = False) -> None:
        inp = self.cmd_input
        v = inp.value
        if final and cand.startswith("/") and not cand.endswith("/"):
            cand += " "  # accepted a /command — ready for its arguments
        inp.value = v[:start] + cand + v[end:]
        inp.cursor_position = start + len(cand)

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
            timeout = max(1, min(int(self.settings.get("default_timeout", 120)),
                                 self.settings.get("max_timeout", 1500)))
            max_out = self.settings.get("max_output_bytes", 256 * 1024)
            deadline = t0 + timeout
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
            total = 0
            truncated = False
            noted = False
            reason = ""
            sel = selectors.DefaultSelector()
            try:
                assert proc.stdout is not None
                sel.register(proc.stdout, selectors.EVENT_READ)
                pending = ""
                while True:
                    # Short polls keep the timeout and x-cancel responsive
                    # even when the process is silent (nothing to unblock on).
                    for key, _mask in sel.select(timeout=0.2):
                        try:
                            raw = os.read(key.fd, 65536)
                        except OSError:
                            raw = b""
                        if not raw:
                            continue
                        pending += raw.decode("utf-8", errors="replace")
                        lines = pending.split("\n")
                        pending = lines.pop()
                        for line in lines:
                            if total < max_out:
                                piece = line + "\n"
                                chunks.append(piece)
                                total += len(piece.encode("utf-8"))
                                if total >= max_out:
                                    truncated = True
                                self.call_from_thread(self._shell_chunk, rid,
                                                      piece)
                            elif not noted:
                                noted = True
                                self.call_from_thread(
                                    self._shell_chunk, rid,
                                    f"… output truncated at {max_out} bytes …\n")
                    if proc.poll() is not None:
                        break
                    if time.time() >= deadline:
                        proc.kill()
                        reason = f"command timed out after {timeout}s"
                        break
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                sel.unregister(proc.stdout)
                try:
                    rest = proc.stdout.read() or ""
                except Exception:
                    rest = ""
                for line in (pending + rest).split("\n"):
                    if total < max_out:
                        piece = line + "\n"
                        chunks.append(piece)
                        total += len(piece.encode("utf-8"))
                truncated = truncated or total >= max_out
            finally:
                sel.close()
                self._shell_procs.pop(rid, None)
            exit_code = proc.returncode
            if not reason and exit_code is not None and exit_code < 0:
                reason = "cancelled"  # killed from outside, e.g. via x
            res = self._shell_result(rid, task, cwd, cmd_text, t0,
                                     ok=not reason and exit_code == 0,
                                     exit_code=exit_code,
                                     output="".join(chunks), error=reason,
                                     truncated=truncated)
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
                      output: str, error: str, truncated: bool = False) -> dict:
        ended = time.time()
        lines = [l for l in output.splitlines() if l.strip()]
        tail = lines[-1][:120] if lines else "no output"
        summary = (f"ok · {len(lines)} line(s) · {tail}" if ok
                   else f"{error or f'exit {exit_code}'}")
        if truncated:
            summary += " · truncated"
        return {
            "id": rid, "ok": ok, "exit": exit_code,
            "stdout": output, "stderr": "", "truncated": truncated,
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
                   "at": time.time(), "session": self.instance_session}
        tmp = os.path.join(MESSAGES_DIR, mid + ".json.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, os.path.join(MESSAGES_DIR, mid + ".json"))
        except OSError:
            self.notify("could not send message")
            return
        card = MessageCard(mid, text, incoming=False, at=payload["at"])
        self._msg_cards[mid] = card
        card.set_status(self._msg_status(mid))
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
                self._refresh_statusbar()
                self.notify(f"cwd → {d}")
            else:
                self.notify(f"no such directory: {arg or '~'}")
        elif name == "clear":
            # Clear the whole feed: task cards, message/reply cards and
            # /help output — anything mounted in the task list. Reply
            # cards and help output are mounted bare (not tracked in
            # self.cards / self._msg_cards), so walk the DOM instead of
            # the tracking dicts. The empty-state widget stays put.
            # The persisted feed state (session history, message/reply
            # files) is wiped too, so a restart stays clear.
            n = 0
            keep = getattr(self, "empty_state", None)
            for child in list(self.task_list.children):
                if child is keep:
                    continue
                try:
                    child.remove()
                except Exception:
                    pass
                n += 1
            self.cards.clear()
            self._msg_cards.clear()
            self.focused_rid = None
            wiped = self._wipe_feed_history()
            try:
                self._refresh_statusbar()
            except Exception:
                pass
            self.notify(f"Cleared {n} card(s), wiped {wiped} history file(s)")
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
                         cwd=self.session_cwd, source="local",
                         session=self.instance_session)
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
            self._toggle_todos()
        elif name == "sidebar":
            self.action_toggle_sidebar()
        elif name == "todo":
            if not arg:
                self._toggle_todos()
            elif arg == "clear" or arg.startswith("clear "):
                self._clear_todos(arg[6:].strip())
            else:
                key = arg if arg.endswith(".md") else arg + ".md"
                rec = self._todo_data.get(key)
                if rec is None:
                    self.notify(f"no todo list: {arg} (see /todos)")
                else:
                    self._sidebar_manual_hide = False
                    self._sidebar_manual_show = True
                    self.expand_todo(key)
                    self.notify(f"todo: {key} · {rec['done']}/{rec['total']}")
        elif name == "skills":
            self._list_skills()
        elif name == "skill":
            self._skill_cmd(arg)
        elif name == "autoapprove":
            if not arg:
                self._set_auto_approve(not self.settings.get("auto_approve", False))
            elif arg == "on":
                self._set_auto_approve(True)
            elif arg == "off":
                self._set_auto_approve(False)
            else:
                self.notify("usage: /autoapprove [on|off]")
        elif name == "heartbeat":
            self._heartbeat_cmd(arg)
        elif name == "export":
            self._export_session(arg)
        elif name == "restart":
            self._restart_self()
        elif name in ("quit", "q"):
            self.exit()
        else:
            self.notify(f"unknown command: /{name}  (try /help)")

    def _restart_self(self) -> None:
        """Restart this TUI: exit and let __main__ re-exec run.sh.

        Picks up new code. State is safe: session history, cards and
        settings are all persisted and restored on launch.
        """
        os.environ["MUSE_CLI_RESTART"] = "1"
        self.notify("restarting…")
        self.exit()

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

    def _esc_tap(self, blur: bool = True) -> None:
        """One Esc press toward the double-Esc failsafe.

        The first tap records the time (and leaves the input box); a
        second tap within DOUBLE_ESC_WINDOW stops everything running.
        """
        now = time.monotonic()
        if now - self._last_esc < self.DOUBLE_ESC_WINDOW:
            self._last_esc = 0.0
            self.action_stop_all()
        else:
            self._last_esc = now
            if blur and isinstance(self.focused, Input):
                self.cmd_input.blur()

    def on_key(self, event: events.Key) -> None:
        """Keyboard-first navigation.

        Single-letter bindings only fire when the input box is NOT focused
        (Textual gives typed characters to the focused Input). Esc leaves
        the input so the keys work; / jumps back into it; j/k move the
        selection between task cards.
        """
        if CommandPalette.is_open(self):
            # The command palette owns the keyboard while open: our
            # global keys (notably Esc, which the palette binds to close
            # itself, and ↑/↓) must not steal from it.
            return
        in_input = isinstance(self.focused, Input)
        if event.key == "escape":
            # Esc with the completion dropdown open is consumed by CmdInput
            # (closes the dropdown, counts the press) and never reaches here.
            self._esc_tap()
            event.prevent_default()
        elif event.key == "shift+tab" and not in_input:
            # CmdInput handles this when the box is focused.
            self.action_toggle_auto_approve()
            event.prevent_default()
        elif event.key == "tab" and not in_input:
            self.cmd_input.focus()
            event.prevent_default()
        elif event.key == "up" and in_input:
            self._hist_move(-1)
            event.prevent_default()
        elif event.key == "down" and in_input:
            self._hist_move(1)
            event.prevent_default()
        elif event.key == "slash" and not in_input:
            self.cmd_input.focus()
            event.prevent_default()
        elif event.key in ("j", "down") and not in_input:
            self._move_selection(1)
            event.prevent_default()
        elif event.key in ("k", "up") and not in_input:
            self._move_selection(-1)
            event.prevent_default()
        elif event.key in ("left_square_bracket", "right_square_bracket") \
                and not in_input:
            # cycle the expanded todo list in the sidebar
            self.cycle_todo(1 if event.key == "right_square_bracket" else -1)
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
        inp = self.cmd_input
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
                     source=card.source, steps=card.steps or None,
                     session=self.instance_session)
        new_card = TaskCard(rid, card.task_text, card.source, card.cmd,
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

    def action_stop_all(self) -> None:
        """Failsafe: double-Esc cancels everything currently running.

        Kills in-process shell tasks and files cancel markers for bridge
        tasks, same as `x` does for one task — just for all of them.
        """
        running = [c for c in self.cards.values() if not c.done]
        if not running:
            self.notify("nothing running")
            return
        for card in running:
            proc = self._shell_procs.get(card.rid)
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
            cancel(card.rid)
        self.notify(f"stopping {len(running)} task(s) — double-Esc failsafe")

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

    # -- layout refresh: topline / modeline / activity / cwdline --
    def _refresh_statusbar(self) -> None:
        """Legacy name kept for call sites; refreshes the whole chrome."""
        self._refresh_topline()
        self._refresh_modeline()
        self._refresh_activity()
        self._refresh_cwdline()

    def _refresh_topline(self) -> None:
        # Header: app title · session · Muse connection status.
        t = Text()
        t.append("muse-cli", style="bold")
        t.append(f"  [{self.instance_session}]", style="bold yellow")
        t.append("   muse ")
        if self._muse_running:
            t.append("● working", style="green")
        else:
            t.append("● connected", style="green")
        wtext, wstyle = self._watcher_segment()
        t.append("   watcher ")
        t.append(wtext, style=wstyle)
        try:
            self.topline.update(t)
        except AttributeError:
            pass

    def _refresh_modeline(self) -> None:
        """Second line after chatbox: mode · Muse link · heartbeat."""
        t = Text()
        if self.settings.get("auto_approve"):
            t.append("● Auto mode", style="yellow")
        else:
            t.append("● Approval mode", style="dim")
        # Muse connection: explicit CLI<->Muse link state from watcher freshness.
        t.append(" · CLI ↔ Muse ")
        if self._muse_running:
            t.append("● working", style="green")
        else:
            wtext, wstyle = self._watcher_segment()
            if wtext == "●" and wstyle == "green":
                t.append("● connected", style="green")
            else:
                t.append(f"● {wtext}", style=wstyle)
        # Heartbeat status.
        hb = self.settings.get("heartbeat_minutes", 0)
        if hb:
            t.append(f" · ♥ {hb}m", style="cyan")
            if self._heartbeat_last:
                ago = int((time.time() - self._heartbeat_last) // 60)
                t.append(f" (last {ago}m ago)", style="dim")
        if os.path.exists(PAUSED_PATH):
            t.append(" · ⏸ paused", style="yellow")
        try:
            self.modeline.update(t)
        except AttributeError:
            pass

    # -- heartbeat: periodic ping to Muse for updates --
    def _heartbeat_cmd(self, arg: str) -> None:
        """/heartbeat [minutes|off] — ping Muse for updates on a timer."""
        arg = (arg or "").strip().lower()
        if not arg:
            hb = self.settings.get("heartbeat_minutes", 0)
            if hb:
                self.notify(f"♥ heartbeat every {hb}m")
            else:
                self._heartbeat_start(5)
                self.notify("♥ heartbeat every 5m — updates will appear here")
            return
        if arg in ("off", "0", "stop", "disable"):
            self._heartbeat_stop()
            self.notify("♥ heartbeat off")
            return
        try:
            mins = int(arg)
            if mins < 1 or mins > 1440:
                raise ValueError
        except ValueError:
            self.notify("usage: /heartbeat <minutes|off> (1–1440)")
            return
        self._heartbeat_start(mins)
        self.notify(f"♥ heartbeat every {mins}m — updates will appear here")

    def _heartbeat_start(self, mins: int) -> None:
        self._heartbeat_stop()
        self.settings["heartbeat_minutes"] = mins
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._heartbeat_timer = self.set_interval(mins * 60,
                                                  self._heartbeat_tick)
        self._refresh_modeline()

    def _heartbeat_stop(self) -> None:
        if self._heartbeat_timer is not None:
            try:
                self._heartbeat_timer.stop()
            except Exception:
                pass
            self._heartbeat_timer = None
        self.settings["heartbeat_minutes"] = 0
        try:
            save_settings(self.settings)
        except Exception:
            pass
        self._refresh_modeline()

    def _heartbeat_tick(self) -> None:
        """Every interval: ask Muse for the latest updates via the inbox."""
        self._heartbeat_last = time.time()
        mid = f"hb-{uuid.uuid4().hex[:8]}"
        payload = {
            "mid": mid,
            "at": time.time(),
            "session": self.instance_session,
            "source": "muse",
            "heartbeat": True,
            "text": ("♥ heartbeat — please reply with any updates: "
                       "task progress, new messages, things needing my "
                       "attention. If nothing new, a brief 'all quiet' is fine."),
        }
        try:
            ensure_dirs()
            tmp = os.path.join(MESSAGES_DIR, mid + ".json.tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, os.path.join(MESSAGES_DIR, mid + ".json"))
        except Exception:
            pass
        self._refresh_modeline()

    # Activity line: a typing-indicator, not a card. Active tasks get an
    # animated amber icon + a state word + the live description in gray;
    # idle gets a static gray "Idle" (never "Waiting for you" — that phrase
    # misleads when the assistant is actually working elsewhere).
    DOUBLE_ESC_WINDOW = 0.7  # seconds between two Esc presses = stop all

    STATE_WORDS = (
        (("test", "spec", "pytest", "vitest", "jest"), "Testing"),
        (("write", "edit", "creat", "patch", "implement"), "Coding"),
        (("run", "exec", "build", "deploy", "install"), "Tinkering"),
    )

    def _state_word(self, card: TaskCard) -> str:
        text = f"{card._step_text or ''} {card._line or ''} {card.task_text}".lower()
        for keywords, word in self.STATE_WORDS:
            if any(k in text for k in keywords):
                return word
        return "Thinking"

    def _refresh_activity(self) -> None:
        """Realtime status above the input: waiting (gray) vs doing (amber)."""
        active = [c for c in self.cards.values()
                  if not c.done and not c.awaiting]
        waiting = sum(1 for c in self.cards.values() if c.awaiting)
        t = Text()
        if active:
            first = max(active, key=lambda c: c.t0)
            el = time.time() - first.t0
            live = (first._step_text or first._line or first.task_text)[:52]
            t.append(SPINNER[self._frame % len(SPINNER)] + " ", style="yellow")
            t.append(self._state_word(first), style="yellow")
            t.append(" · ", style="dim")
            t.append(f"{live} · {el:.0f}s", style="dim")
            if len(active) > 1:
                t.append(f" · +{len(active) - 1} more", style="dim")
        elif waiting:
            t.append(f"◷ {waiting} awaiting approval — a approve · d deny",
                     style="yellow")
        else:
            ext = self._external_activity_text()
            if ext is not None:
                t = ext
            else:
                t.append("○ Idle", style="dim")
        try:
            self.activity_left.update(t)
            # Mini todo summary on the right, above the message box.
            done, total = self._todo_counts()
            self.activity_right.update(
                Text(f"☑ {done}/{total} todos", style="dim") if total else "")
        except AttributeError:
            pass

    def _external_activity_text(self) -> Text | None:
        """Activity this TUI didn't start: other sessions' tasks, the queue,
        parked approvals, and the inbox watcher. None when truly nothing is
        happening — only then does the line read Idle."""
        own = {c.rid for c in self.cards.values()}
        # 1. approvals parked by any bridge instance need a human now.
        try:
            approvals = [f[:-5] for f in os.listdir(APPROVAL_DIR)
                         if f.endswith(".json") and f[:-5] not in own]
        except OSError:
            approvals = []
        if approvals:
            n = len(approvals)
            return Text(f"◷ {n} awaiting approval — a approve · d deny",
                        style="yellow")
        # 2. tasks currently owned by a live bridge instance (any session):
        # claimed queue files exist exactly while the task runs.
        live: list[tuple[str, str]] = []  # (rid, claim path)
        queued = 0
        try:
            qnames = os.listdir(QUEUE_DIR)
        except OSError:
            qnames = []
        for name in qnames:
            base, sep, pid_s = name.rpartition(CLAIM_SUFFIX + ".")
            if sep and base.endswith(".json") and pid_s.isdigit():
                rid = base[:-5]
                if rid in own:
                    continue
                try:
                    alive = pid_alive(int(pid_s))
                except Exception:
                    alive = False
                if alive:
                    live.append((rid, os.path.join(QUEUE_DIR, name)))
            elif name.endswith(".json"):
                queued += 1
        # 3. agent-pushed status lines for tasks we have no card for.
        status: list[tuple[float, str, str]] = []  # (mtime, rid, last line)
        try:
            sfiles = os.listdir(STATUS_DIR)
        except OSError:
            sfiles = []
        for f in sfiles:
            if not f.endswith(".txt") or f[:-4] in own:
                continue
            p = os.path.join(STATUS_DIR, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    lines = [ln.strip() for ln in fh if ln.strip()]
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if lines:
                status.append((mt, f[:-4], lines[-1]))
        if live or status:
            t = Text()
            t.append(SPINNER[self._frame % len(SPINNER)] + " ", style="yellow")
            n = len({rid for rid, _ in live} |
                    {rid for _, rid, _ in status})
            t.append(f"Working · {n} task{'s' if n != 1 else ''}",
                     style="yellow")
            desc = ""
            if status:
                status.sort()
                desc = status[-1][2]
            else:
                for rid, path in live:
                    try:
                        with open(path, encoding="utf-8") as fh:
                            req = json.load(fh)
                        cand = req.get("task") or " ".join(
                            req.get("cmd", []))
                        if cand:
                            desc = cand
                            break
                    except (OSError, ValueError):
                        continue
            if desc:
                t.append(" · ", style="dim")
                t.append(desc[:52], style="dim")
            return t
        # 4. queued work waiting for a bridge.
        if queued:
            return Text(f"◷ {queued} queued", style="dim")
        # 5. the inbox watcher is composing a reply right now.
        w = self._watcher
        if isinstance(w, dict) and w.get("state") == "writing":
            try:
                age = time.time() - float(w.get("at") or 0)
            except (TypeError, ValueError):
                age = float("inf")
            if age <= WATCHER_STALE_S:
                return Text("✎ Muse is writing…", style="yellow")
        return None

    def _refresh_cwdline(self) -> None:
        # Compact: folder • branch ● (green = clean, orange = dirty).
        folder = os.path.basename(os.path.abspath(self.session_cwd))
        branch, dirty = "", False
        try:
            p = subprocess.run(["git", "branch", "--show-current"],
                               cwd=self.session_cwd, capture_output=True,
                               text=True, timeout=2)
            if p.returncode == 0:
                branch = p.stdout.strip()
            p2 = subprocess.run(["git", "status", "--porcelain"],
                                cwd=self.session_cwd, capture_output=True,
                                text=True, timeout=2)
            dirty = bool(p2.stdout.strip())
        except Exception:
            pass
        t = Text()
        t.append(folder, style="bold")
        if branch:
            t.append(" • ", style="dim")
            t.append(branch, style="dim")
            t.append(" ")
            t.append("●", style="dark_orange" if dirty else "green")
        try:
            self.cwdline.update(t)
        except AttributeError:
            pass
