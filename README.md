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
| `c` | show/hide the selected task's commands + full output |
| `x` | cancel the selected (or currently running) task |
| `y` | copy the selected task's detail to the clipboard |
| `g` | jump to the newest task |
| `q` | quit |

Slash commands in the input box: `/cd <dir>`, `/run <script>`, `/scripts`, `/settings`, `/sessions`, `/clear`, `/help`, `/quit`. Aliases: `/exit` = `/quit`, `/q` = `/quit`, `/h` = `/help`, `/resume` = `/sessions`. Click a card to select it. The detail pane is a read-only text area, so you can also drag-select text with the mouse.

## Roadmap ideas

- True PTY streaming for interactive commands
- Approvals: pause a task and ask the user before continuing
- Skills picker wired to `~/.muse/skills/`
- Remote attach: watch the bridge from another machine
