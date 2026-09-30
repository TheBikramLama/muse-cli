"""Stdio MCP server for muse-cli.

Exposes the bridge as MCP tools so any MCP client (Claude Code, Cursor,
future Muse-app support, …) can run terminal commands through the same
guardrailed bridge the TUI uses — same executable allowlist, same queue
protocol, same result files.

Transport: JSON-RPC 2.0 over stdio, newline-delimited (one JSON object
per line, no Content-Length headers) — the framing the MCP spec defines
for its stdio transport. `mcp_main()` is the entry point; the `mcp`
subcommand in `__main__.py` wires it.

The server never executes commands itself: `terminal_run` validates the
request (allowlist + cwd roots, exactly like runner.py), drops it in
`~/.muse/queue/`, and waits for the bridge (TUI or `python -m muse_cli
--daemon`) to claim and run it.
"""
from __future__ import annotations

import json
import os
import sys
import time

from . import activity, auto_todo
from .config import load_settings
from .paths import (DEFAULT_SESSION, QUEUE_DIR, TODOS_DIR, ensure_dirs,
                    valid_session)
from .protocol import cancel as _cancel, read_result, submit, take_result
from .runner import check_cwd

#: MCP protocol version this server speaks.
PROTOCOL_VERSION = "2025-06-18"
SERVER_VERSION = "0.1.0"

#: How long terminal_run waits for a bridge to claim the request before
#: telling the caller nobody is listening.
CLAIM_WAIT_S = 15.0
#: Result-poll interval while terminal_run blocks.
POLL_S = 0.25

# ---------------------------------------------------------------------------
# JSON-RPC plumbing


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _ok(rid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": code, "message": message}}


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Tools


def _result_view(res: dict) -> dict:
    """The fields of a bridge result we hand to MCP clients."""
    return {
        "ok": bool(res.get("ok")),
        "exit": res.get("exit"),
        "stdout": res.get("stdout", ""),
        "stderr": res.get("stderr", ""),
        "truncated": bool(res.get("truncated")),
        "summary": res.get("summary") or res.get("error") or "",
    }


def _tool_terminal_run(args: dict) -> dict:
    settings = load_settings()
    cmd = args.get("cmd")
    if (not isinstance(cmd, list) or not cmd
            or not all(isinstance(a, str) for a in cmd)):
        raise _RpcError(-32602, "'cmd' must be a non-empty list of strings")

    # Same allowlist check as runner.py — never bypassed here either.
    exe = os.path.basename(cmd[0])
    if exe not in settings.get("allowlist", []):
        raise _RpcError(-32602, f"executable '{exe}' is not in the allowlist "
                                f"(edit ~/.muse/settings.json to add it)")

    cwd_arg = args.get("cwd")
    if cwd_arg is not None and not isinstance(cwd_arg, str):
        raise _RpcError(-32602, "'cwd' must be a string")
    _, err = check_cwd(cwd_arg, settings.get("allowed_roots", ["~"]))
    if err:
        raise _RpcError(-32602, err)

    raw_timeout = args.get("timeout")
    if raw_timeout is None:
        timeout = int(settings.get("default_timeout", 120))
    else:
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            raise _RpcError(-32602, "'timeout' must be a number of seconds")
    timeout = max(1, min(timeout, settings.get("max_timeout", 1500)))

    session = args.get("session") or DEFAULT_SESSION
    if not isinstance(session, str) or not valid_session(session):
        raise _RpcError(-32602, f"invalid session name: {session!r}")

    task = args.get("task")
    if task is not None and not isinstance(task, str):
        raise _RpcError(-32602, "'task' must be a string")
    task = task or " ".join(cmd)[:120] or "mcp command"

    # Same request format the bridge expects (see protocol.submit).
    rid = submit(task, cmd=cmd, cwd=cwd_arg, timeout=timeout,
                 source="mcp", session=session)
    queue_path = os.path.join(QUEUE_DIR, rid + ".json")

    start = time.time()
    claim_by = start + CLAIM_WAIT_S
    deadline = start + timeout
    while True:
        res = read_result(rid)
        if res is not None:
            take_result(rid)  # consumed: terminal_result won't see it again
            view = _result_view(res)
            view["request_id"] = rid
            view["timed_out"] = False
            return view
        now = time.time()
        if now >= claim_by and os.path.exists(queue_path):
            raise _RpcError(
                -32000,
                "no bridge claimed the request within 15s — start the TUI "
                "(`./run.sh`) or the daemon (`python -m muse_cli --daemon`)")
        if now >= deadline:
            return {"request_id": rid, "ok": False, "exit": None,
                    "stdout": "", "stderr": "", "truncated": False,
                    "summary": "timed out waiting for the result",
                    "timed_out": True}
        time.sleep(POLL_S)


def _tool_terminal_result(args: dict) -> dict:
    rid = args.get("request_id")
    if not isinstance(rid, str) or not rid:
        raise _RpcError(-32602, "'request_id' must be a non-empty string")
    res = take_result(rid)  # deletes after reading, like a normal client
    if res is None:
        return {"ready": False, "request_id": rid}
    view = _result_view(res)
    view["request_id"] = rid
    view["ready"] = True
    return view


def _tool_terminal_cancel(args: dict) -> dict:
    rid = args.get("request_id")
    if not isinstance(rid, str) or not rid:
        raise _RpcError(-32602, "'request_id' must be a non-empty string")
    _cancel(rid)
    return {"cancelled": True, "request_id": rid}


def _tool_agents_list(args: dict) -> dict:
    entries = activity.read_all()
    agents = sorted(entries.values(),
                    key=lambda r: r.get("at") or 0, reverse=True)
    return {"agents": agents}


def _tool_activity_report(args: dict) -> dict:
    agent = args.get("agent")
    if not isinstance(agent, str) or not agent:
        raise _RpcError(-32602, "'agent' must be a non-empty string")
    state = args.get("state") or ""
    if state not in ("",) + activity.STATES:
        raise _RpcError(-32602,
                        f"'state' must be one of {list(activity.STATES)}")
    kwargs = {}
    for key in ("task", "status", "todo", "label", "reason"):
        val = args.get(key)
        if val is not None:
            if not isinstance(val, str):
                raise _RpcError(-32602, f"'{key}' must be a string")
            kwargs[key] = val
    path = activity.report(agent, state=state, **kwargs)
    return {"ok": True, "path": path}


def _todo_view(name: str, info: dict) -> dict:
    items = info.get("items") or []
    return {
        "name": name,
        "title": info.get("title", ""),
        "owner": info.get("owner", ""),
        "total": len(items),
        "done": sum(1 for _, checked, _ in items if checked),
        "done_marker": info.get("done_marker", ""),
        "mtime": info.get("mtime", 0),
    }


def _tool_todos_list(args: dict) -> dict:
    ensure_dirs()
    todos = []
    try:
        names = sorted(os.listdir(TODOS_DIR))
    except OSError:
        names = []
    for name in names:
        if not auto_todo.is_auto(name):
            continue
        info = auto_todo.parse(os.path.join(TODOS_DIR, name))
        if info is None:
            continue
        todos.append(_todo_view(name, info))
    return {"todos": todos}


def _tool_todos_read(args: dict) -> dict:
    name = args.get("name")
    if (not isinstance(name, str) or not name
            or not auto_todo.is_auto(name) or "/" in name):
        raise _RpcError(-32602, "'name' must be an auto todo filename")
    info = auto_todo.parse(os.path.join(TODOS_DIR, name))
    if info is None:
        raise _RpcError(-32602, f"todo not found: {name}")
    view = _todo_view(name, info)
    view["text"] = info.get("text", "")
    view["items"] = [{"done": checked, "text": text}
                     for _, checked, text in (info.get("items") or [])]
    return view


_TOOLS: dict[str, tuple] = {
    "terminal_run": (
        _tool_terminal_run,
        "Run a terminal command through the muse-cli bridge. The command's "
        "executable must be on the bridge allowlist (~/.muse/settings.json); "
        "the request is queued and this call blocks until the bridge runs it "
        "and the result arrives.",
        {"type": "object",
         "properties": {
             "cmd": {"type": "array", "items": {"type": "string"},
                     "description": "argv list, e.g. [\"git\", \"status\"]"},
             "cwd": {"type": "string",
                     "description": "working directory (must sit under an allowed root)"},
             "timeout": {"type": "number",
                         "description": "max seconds to wait (default 120, capped by settings)"},
             "task": {"type": "string",
                      "description": "human summary shown in the TUI"},
             "session": {"type": "string",
                         "description": "bridge session to route to (default \"main\")"},
         },
         "required": ["cmd"]}),
    "terminal_result": (
        _tool_terminal_result,
        "Fetch a queued result by request id (consumes it; gone afterwards). "
        "Use after a terminal_run that timed out, when the result arrives late.",
        {"type": "object",
         "properties": {"request_id": {"type": "string"}},
         "required": ["request_id"]}),
    "terminal_cancel": (
        _tool_terminal_cancel,
        "Ask the bridge to cancel a queued or running request.",
        {"type": "object",
         "properties": {"request_id": {"type": "string"}},
         "required": ["request_id"]}),
    "agents_list": (
        _tool_agents_list,
        "Live agent presence: activity reports with derived display_state "
        "(working / waiting / stalled / done / failed).",
        {"type": "object", "properties": {}}),
    "activity_report": (
        _tool_activity_report,
        "Heartbeat an agent's activity report (task, status, lifecycle state, "
        "reason, todo heartbeat).",
        {"type": "object",
         "properties": {
             "agent": {"type": "string",
                       "description": "agent id, e.g. \"side-chat:<chat-id>\""},
             "task": {"type": "string"},
             "status": {"type": "string"},
             "todo": {"type": "string"},
             "label": {"type": "string"},
             "state": {"type": "string",
                       "enum": list(activity.STATES)},
             "reason": {"type": "string"},
         },
         "required": ["agent"]}),
    "todos_list": (
        _tool_todos_list,
        "List current auto todo checklists (title, progress, owner).",
        {"type": "object", "properties": {}}),
    "todos_read": (
        _tool_todos_read,
        "Read one auto todo checklist with its full text and items.",
        {"type": "object",
         "properties": {"name": {"type": "string",
                                 "description": "todo filename from todos_list"}},
         "required": ["name"]}),
}


# ---------------------------------------------------------------------------
# JSON-RPC dispatch


def handle_message(msg) -> dict | None:
    """Handle one JSON-RPC message. Returns the response, or None for
    notifications (no id) which get no reply."""
    if not isinstance(msg, dict):
        return _err(None, -32600, "invalid request")
    rid = msg.get("id")
    method = msg.get("method")
    if not isinstance(method, str):
        return _err(rid, -32600, "invalid request")

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "muse-cli", "version": SERVER_VERSION},
        })
    if method == "notifications/initialized":
        return None  # notification: no reply
    if method == "tools/list":
        return _ok(rid, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema}
            for name, (fn, desc, schema) in _TOOLS.items()]})
    if method == "tools/call":
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            return _err(rid, -32602, "invalid params")
        name = params.get("name")
        entry = _TOOLS.get(name) if isinstance(name, str) else None
        if entry is None:
            return _err(rid, -32602, f"unknown tool: {name!r}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _err(rid, -32602, "'arguments' must be an object")
        try:
            return _ok(rid, {"content": [{"type": "text",
                                         "text": json.dumps(entry[0](args))}]})
        except _RpcError as e:
            return _err(rid, e.code, e.message)
        except Exception as e:  # never let a tool crash the server
            return _err(rid, -32603, f"internal error: {e}")
    return _err(rid, -32601, f"method not found: {method}")


def mcp_main(argv: list | None = None) -> int:
    """Run the stdio MCP server until stdin closes. Returns exit code."""
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if not line:
            return 0  # client hung up
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _send(_err(None, -32700, "parse error"))
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:
            rid = msg.get("id") if isinstance(msg, dict) else None
            resp = _err(rid, -32603, f"internal error: {e}")
        if resp is not None:
            _send(resp)
    # unreachable
