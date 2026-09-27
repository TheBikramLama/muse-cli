# muse-cli

A full-screen TUI terminal bridge. Two-way communication between Muse and your Mac — like a coding-agent CLI (claude code / codex style), but the agent on the other end can also be Muse itself.

- **Muse → Mac:** Muse drops a task file into `~/.muse/queue/`; the bridge runs it; the result lands in `~/.muse/results/`.
- **You → Mac:** type in the TUI input box any time — mid-task or after — and watch it run live.
- **Talk to Muse:** plain text in the input box goes to the Muse app as a message; its replies render as Markdown cards right in the feed.
- **Task cards, not command spam:** each unit of work shows a human summary with a live spinner, elapsed time, and a streaming output tail that updates *in place*. Commands stay hidden until you press `c`; every finished task carries a one-line summary of what it did.
- **Live link:** the status bar shows whether the Muse app is routing work through the bridge (● working / ○ idle) and whether the inbox watcher answering your messages is alive (`watcher ●` fresh / `⚠ silent` stale / `⚠ error`). Press `x` to cancel a running or queued task.

Replaces the old `muse-runner.py`. Unlike the old runner, it is not locked to one folder — commands may run anywhere under `~` by default (configurable).

## Quickstart

```bash
./run.sh                          # creates .venv on first run, then launches the TUI
./run.sh --session work           # named instance — run parallel TUIs side by side
./run.sh --daemon                 # bridge only, no TUI (plain stdout log)
```

## How Muse uses it

Write a request JSON to `~/.muse/queue/<uuid>.json`:

```json
{
  "id": "<uuid>",
  "task": "Run backend test suite",
  "cmd": ["php", "artisan", "test"],
  "cwd": "/Users/bikram/projects/lipi/api.lipi.com.np",
  "timeout": 600,
  "source": "muse"
}
```

Poll `~/.muse/results/<uuid>.json` until it appears, read it, delete it. Result:

```json
{
  "id": "<uuid>", "ok": true, "exit": 0,
  "stdout": "...", "stderr": "...", "truncated": false,
  "summary": "ok · 47 line(s) · Tests: 47 passed",
  "duration_s": 12.4, "task": "Run backend test suite", "source": "muse"
}
```

- `task` is the human-readable summary shown in the TUI — always set it.
- `cmd` is an argv list, never a shell string.
- `source` is `"muse"` or `"local"`; `reveal: true` shows commands immediately.
- `"session": "work"` routes the request to the TUI started with `--session work` (default `"main"`). Each instance only runs its own session's requests.
- `{"id": "<uuid>", "ping": true}` is a liveness check — the bridge answers `{"ok": true, "pong": true}` with no task card.

Guardrails (live-editable in `~/.muse/settings.json`): executable allowlist, allowed cwd roots, timeouts, output caps. `GIT_TERMINAL_PROMPT=0` is set so git never hangs on credentials.

## `~/.muse` layout

```
~/.muse/
├── settings.json   # allowlist, allowed_roots, timeouts, tui prefs (live-reloaded)
├── queue/          # inbound task files
├── results/        # finished results
├── cancel/         # drop an empty file named <task-id> to cancel it
├── approval/       # parked approval requests
├── messages/       # your plain-text messages to Muse
├── replies/        # Muse's replies, rendered as cards
├── watcher.json    # inbox-watcher heartbeat (liveness for the TUI)
├── todos/          # live Markdown checklists from Muse
├── sessions/       # JSONL history, one file per app run
├── skills/         # your skills (each in <name>/SKILL.md)
├── scripts/        # your scripts — run one with /run <name> in the TUI
├── exports/        # saved task details / session exports
└── input_history   # ↑/↓ history for the input box
```

## TUI

| Key | Action |
|-----|--------|
| `j` / `k` | move selection between task cards |
| `/` | jump back to the input box (`Esc` leaves it so keys work) |
| `c` | show/hide the selected task's commands + full output |
| `x` | cancel the selected (or currently running) task |
| `p` | pause/resume the bridge queue |
| `r` | retry the selected finished task |
| `a` | approve the selected task (when awaiting approval) |
| `d` | deny the selected task (when awaiting approval) |
| `A` | toggle auto-approve — approval requests run without asking |
| `y` | copy the selected task's detail to the clipboard |
| `s` | save the selected task's detail to `~/.muse/exports/` |
| `g` | jump to the newest task |
| `q` | quit |

Slash commands in the input box: `/cd <dir>`, `/run <script>`, `/scripts`, `/settings`, `/sessions`, `/session <name>`, `/export [name]`, `/todos`, `/todo <name>`, `/autoapprove [on|off]`, `/skills`, `/skill <show|install|remove> <name>`, `/clear`, `/help`, `/quit`. Aliases: `/exit` = `/quit`, `/q` = `/quit`, `/h` = `/help`, `/resume` = `/sessions`. Click a card to select it. The detail pane is a read-only text area, so you can also drag-select text with the mouse.

## Input / output polish

- ↑/↓ in the input box walks command history (persisted in `~/.muse/input_history`).
- `s` saves a task's detail + full output to `~/.muse/exports/<id>.md`.
- Set `tui.notify_on_done: true` in `~/.muse/settings.json` for a macOS notification whenever a task finishes.

## Sessions

`/sessions` lists past sessions, `/session <name>` switches the history log new tasks are written to, and `/export [name]` writes the finished tasks as markdown to `~/.muse/exports/`.

## Parallel instances (`--session`)

Run several TUIs at once, one per named session:

```bash
./run.sh --session work
./run.sh --session lipi
```

- Each instance claims a per-session lock (`~/.muse/muse-cli.<session>.pid`); starting the same session twice exits instead of double-running the queue. Session `main` keeps the historic `~/.muse/muse-cli.pid` path.
- Queue items, plain-text messages and Muse replies carry a `"session"` tag, so each window only runs and shows its own work. Untagged items route to `main` (backwards compatible).
- Claiming a queue file is atomic — parallel instances never execute the same request twice. If an instance dies mid-task, the next start re-queues its claimed file.
- The status bar shows the instance name: `muse-cli · [work]`.
- `/session <name>` is unchanged: it only switches the history log bucket new tasks are written to, not the instance.

## Queue controls

`p` pauses the bridge (queue held, status bar shows `⏸ paused`); `p` again resumes. `r` retries the selected finished task as a fresh card.

## Background daemon & per-session instances

One bridge (TUI or daemon) runs per session — the first claims its session lock, and a second start of the same session exits instead of double-running the queue. Different sessions run side by side freely.

- `./run.sh --daemon` runs the bridge headless (plain stdout logging); add `--session <name>` for a named session.
- `./run.sh --install-launchd` installs it as a macOS LaunchAgent (`com.muse.cli`, logs to `~/.muse/daemon.log`); `--uninstall-launchd` removes it.
- While the daemon holds a session, the TUI refuses to start that same session — unload the daemon first to use the TUI for it.

## Approvals

A request with `"needs_approval": true` is parked in `~/.muse/approval/` instead of running. The TUI shows it as `⏸ awaiting approval` — press `a` to approve (it queues and runs) or `d` to deny (`denied by user` result). Parked requests survive a restart and reappear on launch. (Headless `--daemon` can't approve; requests just wait.)

## Multi-step tasks

A request can carry `steps: [{"name": ..., "cmd": [...], "cwd": ...}]` instead of a single `cmd`. The bridge runs them sequentially inside one task card, stops at the first failing step, and reports per-step outcomes. The card shows `▸ 2/4 · step name` live; the result summary reads `ok · 4/4 steps` or `failed at step 2/4 (name): reason`. Plain single-command requests are unchanged.

## Input modes

The input box routes on its first character:

- `/help` — slash commands (see the TUI table above).
- `!ls -la` — runs a shell command via `bash -lc` in a task card: streaming, cancellable with `x`, cwd-guardrailed, and logged. Output is capped (`max_output_bytes`, default 256 KB — over-long output is truncated with a note), and a real deadline is enforced: a hung command is killed at `timeout` seconds instead of hanging the bridge.
- anything else — sends a message to Muse. It lands in `~/.muse/messages/<id>.json`; Muse's replies are polled from `~/.muse/replies/` about once a second and rendered as Markdown cards. Recent messages are restored at startup. Each outgoing message card shows its state: `sent · waiting for Muse` → `Muse is writing…` → `replied ✓`.

## Todo lists

Muse can drop Markdown checklists into `~/.muse/todos/<name>.md`. The TUI watches the directory (~1/s) and renders each file as a live card: the Markdown body plus checkbox progress (`- [x]` counts as done, shown as `n/m`). `/todos` lists them, `/todo <name>` jumps to one.

## Auto-approve

Requests with `"needs_approval": true` park in `~/.muse/approval/` until you press `a`/`d`. Press `A` (or `/autoapprove on`) to skip the asking — approval requests run straight through. Persists in `~/.muse/settings.json` (`auto_approve`, default off); the status bar shows the state.

## Skills

Skills live in `~/.muse/skills/<name>/SKILL.md` — plain Markdown with `name:`/`description:` frontmatter (no YAML dependency).

- `/skills` — `○` installed, `●` referenced by a running task
- `/skill show <name>` — read the SKILL.md
- `/skill install <git-url|local-path>` — shallow-clone or copy in (must contain a root `SKILL.md`)
- `/skill remove <name>`

Task requests can carry `"skills": ["name", …]`; the card shows a `⚙` badge and the names persist into results and session history. Note: this is the SKILL.md + frontmatter convention only, not the full Claude marketplace/plugin system.

## Muse inbox watcher

Plain-text messages only become a conversation if something on the Muse side reads them. A scheduled job (every ~2 minutes) watches `~/.muse/messages/`, treats each new file as your instruction, does the work — via the same `~/.muse/queue/` bridge when it needs your Mac — and writes a Markdown reply to `~/.muse/replies/<id>.json`, which the TUI renders as a card. Each message is answered exactly once (processed IDs are tracked); your outgoing files are kept so the startup history view still works. Replies echo the message's `"session"` tag so they land in the right window.

The watcher also writes a heartbeat to `~/.muse/watcher.json` on every run (`at`, `ok`, `state` = `idle`/`writing`, `mid`, `error`). The TUI reads it for the status-bar watcher segment: `●` fresh, `✎ writing…` while it answers, `⚠ silent Nm` when no heartbeat arrived for over 5 minutes, `⚠ error` on a recorded failure, `○ not seen` before the first run. A dead watcher can't write anything, so a stale heartbeat is itself the down signal — that's how the TUI tells "no reply yet" apart from "nobody's listening".

## Roadmap ideas

- True PTY streaming for interactive commands
- Approvals: pause a task and ask the user before continuing
- Remote attach: watch the bridge from another machine