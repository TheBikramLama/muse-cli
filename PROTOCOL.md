# muse-cli Protocol

How an AI assistant (Muse or any agent) talks to a running muse-cli instance.
Everything happens through files under `~/.muse/` — no sockets, no network.
All writes should be atomic (write temp file, then rename).

## Directory layout

| Path | Purpose |
|---|---|
| `~/.muse/queue/` | Drop task requests here: `<uuid>.json` |
| `~/.muse/results/` | Bridge writes results here: `<uuid>.json` (same id) |
| `~/.muse/status/` | **Realtime status:** append human-readable progress lines to `<task_id>.txt` while a task runs; the TUI shows the latest line live on the card |
| `~/.muse/messages/` | User-typed messages for the assistant: `<mid>.json` |
| `~/.muse/replies/` | Assistant replies the TUI displays: `<mid>.json` (same id) |
| `~/.muse/cancel/` | Touch `<task_id>` (empty file) to cancel a running/queued task |
| `~/.muse/approval/` | Requests with `needs_approval: true` wait here for the user (`a`/`d`) |
| `~/.muse/watcher.json` | **Heartbeat:** the assistant side writes `{"at": <epoch>, "ok": true, "state": "idle"\|"writing", "mid": <id>\|null, "error": null}` every run. The TUI renders the `CLI ↔ Muse` link from its freshness (stale > 300s) |
| `~/.muse/cli-identity.json` | CLI identity: stable machine id (written by `muse-cli setup`) |
| `~/.muse/paired.json` | Pairing record: which Muse app completed the handshake |
| `~/.muse/pairing/` | Live handshake: `request.json` (code + nonce), `receipt.json` (nonce echo) |
| `~/.muse/todos/` | Todo lists the sidebar renders |
| `~/.muse/sessions/` | Persisted session history |
| `~/.muse/tui-cmd/` | Remote-control ops for the TUI (`{"op": ...}`, session-tagged) |

## Running a task

Write `~/.muse/queue/<uuid>.json`:

```json
{
  "id": "<uuid>",
  "task": "Human summary shown on the card",
  "cmd": ["python3", "-m", "pytest", "-x"],
  "cwd": "/path/to/project",
  "timeout": 120,
  "source": "muse",
  "session": "main"
}
```

- `task`: short human summary (shown on the card; `cmd` stays hidden until the user presses `c`).
- `cmd`: argv list. Omit / use `steps` for multi-step work.
- `steps`: `[{"name": "...", "cmd": [...], "cwd": "..."}]` — run sequentially in one card, stops at first failure.
- `needs_approval: true`: parks in `~/.muse/approval/`; the user approves (`a`) or denies (`d`) in the TUI.
- `session`: routes to the TUI started with `--session <name>` (default `main`).
- `{"ping": true}` → the bridge answers `{"pong": true}` (liveness check).

Then poll `~/.muse/results/<uuid>.json`:

```json
{
  "id": "<uuid>",
  "ok": true,
  "summary": "one-line outcome",
  "stdout": "...",
  "stderr": "...",
  "truncated": false,
  "duration_s": 3.2
}
```

Multi-step results carry a `steps` array with per-step `ok`/`summary`.
Delete the result file after reading it.

## Realtime status (the live line)

While a task runs, the agent may write progress to `~/.muse/status/<task_id>.txt`
— plain text, one update per line. The TUI shows the **latest non-empty line**
on the task card, replacing the generic spinner text. If this TUI has no card
for the task (another session or agent started it), the latest line appears in
the activity statusline instead. Example:

```
cloning repo…
installing dependencies…
running test suite (42 tests)…
```

- Use the **same id** as the queued task.
- Keep lines short (< 80 chars) and human — they render verbatim.
- The TUI polls on its tick; no acknowledgement is sent back.
- The file is deleted automatically when the task finishes.

This is how the card reflects what the agent is *actually doing* instead of
a static "Waiting for you".

## Todos — update as you go (maximum visibility)

`~/.muse/todos/*.md` are Markdown checklists rendered live in the TUI's todo
sidebar and summarized on the statusline (`☑ 2/4 todos`). They are the user's
realtime view of your progress — **update them as you go**, don't just report
at the end.

- **Bridge tasks:** when your request starts, the bridge auto-creates
  `~/.muse/todos/_auto_<slug>-<id>.md` from your request's `steps` (a single
  item for a plain command) and checks items off as steps complete. For
  finer-grained progress, rewrite the same file yourself, flipping
  `- [ ]` to `- [x]` as you finish things. Find yours by the
  `<!-- auto:rid=<your-task-id> -->` marker. Never touch another task's
  `_auto_` file.
- **Conversational work** (no bridge request): create your own
  `~/.muse/todos/_auto_<slug>-<your-id>.md` with a first line of
  `<!-- auto:rid=<your-id> owner=<your-agent-id> t0=<epoch> -->` followed by
  `# Your task title`. The sidebar shows the `# title` — write a real one,
  and write real step labels (never "step 1", "step 2").
- Format: `# Title` under the marker line, then `- [ ]` / `- [x]` lines.
  Parallel agents use separate files — never merge two tasks into one list.
- **Heartbeat:** the file's mtime is your lease. Touch it (any update
  counts) at least every few minutes while the task is live — an activity
  report naming the file does this for you. A list nobody touched for 30
  minutes is treated as abandoned and swept automatically.
- **When done:** append `<!-- auto:done=ok -->` (or just delete the file).
  Don't leave a stale `2/4`. Completed lists linger ~5 minutes for
  visibility, then vanish on their own.

## Activity reports — be visible while you work (companion, not terminal)

The bridge only sees terminal commands. File reads, edits, thinking —
none of it reaches the TUI unless you say something. Say something:

```bash
muse-cli report --agent <your-agent-id> --task "Fixing dashboard drag" \
  --status "editing DashboardPage.tsx" --todo _auto_<slug>-<id>.md \
  --state working --label "dashboard drag bugs"
```

(or drop the same JSON at `~/.muse/activity/<agent-id>.json` yourself:
`{"agent": ..., "label": ..., "task": ..., "status": ..., "todo": ...,
"state": "working", "reason": "", "t0": <epoch>, "at": <epoch>}`).

- Re-run it as your status changes (at least every couple of minutes).
  Fresh reports (< 2 min) render live in the activity line and the
  ⚡ agents sidebar panel, with elapsed time.
- `--state` is your lifecycle: `working` (actively working), `waiting`
  (blocked on the user — say why with `--reason`; the TUI nudges the
  user once per waiting episode), `stalled`, `done`, `failed`. Report it
  honestly; the sidebar shows each state with its own icon and color.
- `--todo` heartbeats your checklist (see above).
- Going quiet for 15 minutes drops you from the UI. A `working` agent
  quiet for 5+ minutes is auto-flagged **stalled**. Silence is the off
  switch — but `--done` is the polite one: it marks you finished and you
  stay visible as done for ~5 minutes. Deleting the file signs you off
  immediately instead.
- `<your-agent-id>` should be stable for the task, e.g.
  `side-chat:<chat-id>` or `subagent:<purpose>-<short>`. `--label` is
  your display name — use your side-chat's title so the sidebar reads
  like the chat list, not a UUID.

## Messaging the user

The user types plain text in the TUI input box → `~/.muse/messages/<mid>.json`:

```json
{"id": "<mid>", "from": "tui", "text": "...", "at": <epoch>, "session": "main"}
```

Reply by writing `~/.muse/replies/<mid>.json`:

```json
{"id": "<mid>", "from": "muse", "text": "Markdown reply", "at": <epoch>, "session": "main"}
```

The reply renders as a Markdown card in the TUI feed. `session` is required —
without it the reply only appears in the `main` window.

## Heartbeat (connection status)

The assistant side **must** write `~/.muse/watcher.json` on every poll loop,
even when there is nothing to do — a stale or missing file *is* the down signal:

```json
{"at": 1759000000, "ok": true, "state": "idle", "mid": null, "error": null}
```

- Run start: `state: "idle"`.
- Before composing a reply: `state: "writing", "mid": "<that message's id>"`.
- Run end: back to `idle` with a fresh `at`.
- On failure: `{"at": <now>, "ok": false, "state": "idle", "mid": null, "error": "<short reason>"}`.

The TUI renders `CLI ↔ Muse ● connected` (fresh), `⚠ silent Nm` (stale),
`⚠ error`, or `○ not seen` (never). `muse-cli doctor` reports the same outside
the TUI.

## Pairing a new machine

1. User installs: `curl -fsSL https://raw.githubusercontent.com/TheBikramLama/muse-cli/main/install.sh | bash`
2. User runs `muse-cli setup` (or just launches `muse-cli`) → the CLI
   creates `~/.muse/pairing/request.json` holding a human-readable code
   plus a random nonce, and prints a copyable prompt.
3. User pastes the prompt into the Muse app. The assistant:
   - reads `pairing/request.json`, confirms the code,
   - writes `pairing/receipt.json` echoing the nonce back — this proves
     it reached *this* machine's bridge,
   - sets up its ~2-minute poll loop (queue + messages) and heartbeat
     writes, per the prompt's watcher instructions.
4. The CLI validates code + nonce, records the pairing in
   `~/.muse/paired.json`, and deletes the handshake files.
   `muse-cli doctor` reports the paired state.

The nonce echo proves liveness: a stale or copied receipt can't pair.
`muse-cli unpair` wipes the pairing (and the CLI identity) so the next
launch re-enters the flow — the equivalent of logging out.

## `muse-cli exec` (synchronous wrapper)

`muse-cli exec [--cwd DIR] [--timeout SEC] [--session NAME] -- <cmd...>`
does the queue → poll → read → delete dance in one call, for scripts and
agents that want a blocking command. (`--` is the documented separator;
without it the command is taken as-is when unambiguous.)

- Validates `cmd[0]`'s basename against the settings allowlist (the same
  check `runner.py` runs) and exits `2` on violation — nothing is queued.
- Queues with `source: "muse-cli-exec"`, default session `"main"`, timeout
  `min(requested or default_timeout, max_timeout)`.
- Waits for the bridge to claim the request (the `<id>.json.claimed.<pid>`
  marker). If nothing claims it within 15s, exits `3` with a "no bridge"
  hint and leaves the queue file in place.
- On result: prints stdout/stderr, deletes the result file, exits with the
  command's exit code (`1` when the result carries none).
- On timeout: drops a cancel file in `~/.muse/cancel/`, prints the request
  id so the late result can be picked up from
  `~/.muse/results/<id>.json`, and exits `124`.

## MCP server (stdio)

`muse_cli/mcp.py` exposes the bridge as an MCP server (`muse-cli mcp`), so
MCP clients can run terminal commands through the same guardrailed bridge
the TUI uses. The server never executes anything itself — it validates,
queues, and waits.

- Transport: JSON-RPC 2.0 over stdio, newline-delimited (one JSON object
  per line, no Content-Length headers) — the framing the MCP spec defines
  for its stdio transport.
- `initialize` returns `protocolVersion`, `capabilities.tools`, and
  `serverInfo` (`name: "muse-cli"`). `tools/list` and `tools/call` are
  supported; `notifications/initialized` is tolerated silently. Unknown
  methods → `-32601`, bad params → `-32602`, unparseable input → `-32700`.
- Tools:
  - `terminal_run(cmd, cwd?, timeout?, task?, session?)` — validates
    `cmd[0]`'s basename against the bridge executable allowlist and `cwd`
    against the allowed roots (the same checks as `runner.py`; violations
    are `-32602` errors), queues the request with `source: "mcp"`, and
    blocks until the result file appears or `timeout` elapses (default
    from settings, capped by `max_timeout`). Returns `stdout`, `stderr`,
    `exit`, `timed_out`, `request_id`. If no bridge claims the request
    within 15s it returns `-32000`: start the TUI (`./run.sh`) or the
    daemon (`python -m muse_cli --daemon`). The result file is consumed.
  - `terminal_result(request_id)` — picks up a result that arrived after a
    `terminal_run` wait timed out; `{ready: false}` when nothing is there.
    Consumes the result file, like a normal client would.
  - `terminal_cancel(request_id)` — drops the cancel file in
    `~/.muse/cancel/`.
  - `agents_list()` — activity reports with derived `display_state`.
  - `activity_report(agent, task?, status?, state?, reason?, label?, todo?)`
    — same lifecycle `state` set as `muse-cli report`.
  - `todos_list()` / `todos_read(name)` — auto todo checklists: progress
    summary, then full text and items.
- The allowlist is never bypassed: the server checks it before queueing,
  and the bridge checks it again at execution time.
