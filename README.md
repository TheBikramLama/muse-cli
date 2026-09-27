# muse-cli

A full-screen TUI terminal bridge. Two-way communication between Muse and your Mac — like a coding-agent CLI (claude code / codex style), but the agent on the other end can also be Muse itself.

- **Muse → Mac:** Muse drops a task file into `~/.muse/queue/`; the bridge runs it; the result lands in `~/.muse/results/`.
- **You → Mac:** type in the TUI input box any time — mid-task or after — and watch it run live.
- **Task cards, not command spam:** each unit of work shows a human summary with a live spinner, elapsed time, and a streaming output tail that updates *in place*. Commands stay hidden until you press `c`; every finished task carries a one-line summary of what it did.
- **Live link:** the status bar shows whether the Muse app is routing work through the bridge (● working / ○ idle). Press `x` to cancel a running or queued task.

Replaces the old `muse-runner.py` (kept under `legacy/` for reference). Unlike the old runner, it is not locked to one folder — commands may run anywhere under `~` by default (configurable).

## Quickstart

```bash
./run.sh                          # creates .venv on first run, then launches the TUI
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

Guardrails (live-editable in `~/.muse/settings.json`): executable allowlist, allowed cwd roots, timeouts, output caps. `GIT_TERMINAL_PROMPT=0` is set so git never hangs on credentials.

## `~/.muse` layout

```
~/.muse/
├── settings.json   # allowlist, allowed_roots, timeouts, tui prefs (live-reloaded)
├── queue/          # inbound task files
├── results/        # finished results
├── cancel/         # drop an empty file named <task-id> to cancel it
├── sessions/       # JSONL history, one file per app run
├── skills/         # your skills
└── scripts/        # your scripts — run one with /run <name> in the TUI
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
| `y` | copy the selected task's detail to the clipboard |
| `s` | save the selected task's detail to `~/.muse/exports/` |
| `g` | jump to the newest task |
| `q` | quit |

Slash commands in the input box: `/cd <dir>`, `/run <script>`, `/scripts`, `/settings`, `/sessions`, `/clear`, `/help`, `/quit`. Aliases: `/exit` = `/quit`, `/q` = `/quit`, `/h` = `/help`, `/resume` = `/sessions`. Click a card to select it. The detail pane is a read-only text area, so you can also drag-select text with the mouse.

## Input / output polish

- ↑/↓ in the input box walks command history (persisted in `~/.muse/input_history`).
- `s` saves a task's detail + full output to `~/.muse/exports/<id>.md`.
- Set `tui.notify_on_done: true` in `~/.muse/settings.json` for a macOS notification whenever a task finishes.

## Sessions

`/sessions` lists past sessions, `/session <name>` switches the session new tasks are logged to, and `/export [name]` writes the finished tasks as markdown to `~/.muse/exports/`.

## Queue controls

`p` pauses the bridge (queue held, status bar shows `⏸ paused`); `p` again resumes. `r` retries the selected finished task as a fresh card.

## Background daemon & single instance

Only one bridge (TUI or daemon) runs at a time — the first one claims `~/.muse/muse-cli.pid`, and a second start exits instead of double-running the queue.

- `./run.sh --daemon` runs the bridge headless (plain stdout logging).
- `./run.sh --install-launchd` installs it as a macOS LaunchAgent (`com.muse.cli`, logs to `~/.muse/daemon.log`); `--uninstall-launchd` removes it.
- While the daemon runs, the TUI refuses to start — unload the daemon first to use the TUI.

## Approvals

A request with `"needs_approval": true` is parked in `~/.muse/approval/` instead of running. The TUI shows it as `⏸ awaiting approval` — press `a` to approve (it queues and runs) or `d` to deny (`denied by user` result). Parked requests survive a restart and reappear on launch. (Headless `--daemon` can't approve; requests just wait.)

## Multi-step tasks

A request can carry `steps: [{"name": ..., "cmd": [...], "cwd": ...}]` instead of a single `cmd`. The bridge runs them sequentially inside one task card, stops at the first failing step, and reports per-step outcomes. The card shows `▸ 2/4 · step name` live; the result summary reads `ok · 4/4 steps` or `failed at step 2/4 (name): reason`. Plain single-command requests are unchanged.

## Roadmap ideas

- True PTY streaming for interactive commands
- Approvals: pause a task and ask the user before continuing
- Skills picker wired to `~/.muse/skills/`
- Remote attach: watch the bridge from another machine
