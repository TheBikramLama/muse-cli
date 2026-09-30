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
