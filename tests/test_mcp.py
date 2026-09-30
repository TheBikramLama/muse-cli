"""Tests for the stdio MCP server (muse_cli.mcp). All hermetic: tmp dirs,
monkeypatched paths and settings — no real bridge needed."""
import io
import json
import os
import sys

import pytest

from muse_cli import activity, auto_todo, mcp, protocol


@pytest.fixture
def mcp_home(tmp_path, monkeypatch):
    """Isolated ~/.muse layout for the MCP server."""
    q = tmp_path / "queue"
    r = tmp_path / "results"
    c = tmp_path / "cancel"
    a = tmp_path / "activity"
    t = tmp_path / "todos"
    for d in (q, r, c, a, t):
        d.mkdir()
    monkeypatch.setattr(mcp, "QUEUE_DIR", str(q))
    monkeypatch.setattr(mcp, "TODOS_DIR", str(t))
    monkeypatch.setattr(protocol, "QUEUE_DIR", str(q))
    monkeypatch.setattr(protocol, "RESULTS_DIR", str(r))
    monkeypatch.setattr(protocol, "CANCEL_DIR", str(c))
    monkeypatch.setattr(activity, "ACTIVITY_DIR", str(a))
    monkeypatch.setattr(auto_todo, "TODOS_DIR", str(t))
    monkeypatch.setattr(
        mcp, "load_settings",
        lambda: {"allowlist": ["echo", "git"],
                 "allowed_roots": ["~"],
                 "default_timeout": 120,
                 "max_timeout": 1500})
    return {"queue": q, "results": r, "cancel": c}


def _call(name, arguments, mid=1):
    return mcp.handle_message({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})


def _text(resp):
    """Unwrap the MCP content envelope back to the tool's dict."""
    assert "result" in resp, resp
    return json.loads(resp["result"]["content"][0]["text"])


# -- handshake / dispatch ----------------------------------------------------

def test_initialize():
    resp = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {}})
    assert resp["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert resp["result"]["serverInfo"]["name"] == "muse-cli"
    assert "tools" in resp["result"]["capabilities"]


def test_initialized_notification_gets_no_reply():
    assert mcp.handle_message({"jsonrpc": "2.0",
                               "method": "notifications/initialized"}) is None


def test_unknown_method():
    resp = mcp.handle_message({"jsonrpc": "2.0", "id": 2, "method": "nope"})
    assert resp["error"]["code"] == -32601


def test_invalid_request_shape():
    assert mcp.handle_message([1, 2])["error"]["code"] == -32600


def test_unknown_tool():
    resp = _call("frobnicate", {})
    assert resp["error"]["code"] == -32602


def test_tools_list_has_terminal_tools():
    resp = mcp.handle_message({"jsonrpc": "2.0", "id": 3,
                               "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert {"terminal_run", "terminal_result", "terminal_cancel",
            "agents_list", "activity_report", "todos_list",
            "todos_read"} <= names
    for t in resp["result"]["tools"]:
        assert t["inputSchema"]["type"] == "object"


# -- terminal_run validation -------------------------------------------------

def test_terminal_run_rejects_non_allowlisted_exe(mcp_home):
    resp = _call("terminal_run", {"cmd": ["rm", "-rf", "/"]})
    assert resp["error"]["code"] == -32602
    assert "allowlist" in resp["error"]["message"]


def test_terminal_run_rejects_bad_cmd_shape(mcp_home):
    resp = _call("terminal_run", {"cmd": "echo hi"})
    assert resp["error"]["code"] == -32602
    resp = _call("terminal_run", {"cmd": []})
    assert resp["error"]["code"] == -32602


def test_terminal_run_rejects_bad_cwd(mcp_home):
    resp = _call("terminal_run", {"cmd": ["echo", "hi"], "cwd": "/nope"})
    assert resp["error"]["code"] == -32602


def test_terminal_run_queues_and_errors_when_no_bridge(mcp_home, monkeypatch):
    monkeypatch.setattr(mcp, "CLAIM_WAIT_S", 0.01)
    resp = _call("terminal_run", {"cmd": ["echo", "hi"],
                                  "task": "say hi"})
    assert resp["error"]["code"] == -32000
    assert "./run.sh" in resp["error"]["message"]
    # ...but it did write a well-formed bridge request first
    queued = os.listdir(str(mcp_home["queue"]))
    assert len(queued) == 1
    with open(os.path.join(str(mcp_home["queue"]), queued[0])) as f:
        req = json.load(f)
    assert req["cmd"] == ["echo", "hi"]
    assert req["task"] == "say hi"
    assert req["source"] == "mcp"
    assert req["session"] == "main"


# -- terminal_result / terminal_cancel ---------------------------------------

def test_terminal_result_round_trip(mcp_home):
    protocol.write_result({"id": "r1", "ok": True, "exit": 0,
                           "stdout": "hi\n", "stderr": "",
                           "truncated": False, "summary": "ok"})
    resp = _text(_call("terminal_result", {"request_id": "r1"}))
    assert resp["ready"] is True
    assert resp["stdout"] == "hi\n"
    assert resp["exit"] == 0
    # consumed: gone afterwards
    assert _text(_call("terminal_result",
                       {"request_id": "r1"}))["ready"] is False


def test_terminal_result_missing(mcp_home):
    resp = _text(_call("terminal_result", {"request_id": "nope"}))
    assert resp == {"ready": False, "request_id": "nope"}


def test_terminal_cancel_drops_file(mcp_home):
    resp = _text(_call("terminal_cancel", {"request_id": "abc123"}))
    assert resp == {"cancelled": True, "request_id": "abc123"}
    assert os.path.exists(os.path.join(str(mcp_home["cancel"]), "abc123"))


# -- agents / activity -------------------------------------------------------

def test_activity_report_round_trip(mcp_home):
    resp = _text(_call("activity_report",
                       {"agent": "side-chat:1", "task": "Fix thing",
                        "status": "editing", "state": "waiting",
                        "reason": "need approval", "label": "my chat"}))
    assert resp["ok"] is True
    with open(resp["path"]) as f:
        rec = json.load(f)
    assert rec["task"] == "Fix thing"
    assert rec["state"] == "waiting"
    assert rec["reason"] == "need approval"


def test_activity_report_rejects_bad_state(mcp_home):
    resp = _call("activity_report", {"agent": "a1", "state": "napping"})
    assert resp["error"]["code"] == -32602


def test_agents_list_reflects_report(mcp_home):
    activity.report("side-chat:9", task="Build x", state="working",
                    label="chat nine")
    resp = _text(_call("agents_list", {}))
    by_id = {a["agent"]: a for a in resp["agents"]}
    assert by_id["side-chat:9"]["display_state"] == "working"
    assert by_id["side-chat:9"]["task"] == "Build x"


# -- todos -------------------------------------------------------------------

def test_todos_list_and_read(mcp_home):
    path = auto_todo.create("rid1", "My task", ["step a", "step b"],
                            owner="tester")
    name = os.path.basename(path)
    listed = _text(_call("todos_list", {}))["todos"]
    assert len(listed) == 1
    assert listed[0]["name"] == name
    assert listed[0]["title"] == "My task"
    assert listed[0]["total"] == 2
    assert listed[0]["done"] == 0

    read = _text(_call("todos_read", {"name": name}))
    assert read["title"] == "My task"
    assert "# My task" in read["text"]
    assert [i["text"].strip() for i in read["items"]] == ["step a", "step b"]


def test_todos_read_rejects_unknown(mcp_home):
    resp = _call("todos_read", {"name": "_auto_nope-x.md"})
    assert resp["error"]["code"] == -32602


# -- stdio loop ---------------------------------------------------------------

def test_mcp_main_stdio_loop(mcp_home, monkeypatch):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "agents_list", "arguments": {}}},
    ]
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in msgs))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert mcp.mcp_main([]) == 0
    lines = [json.loads(ln) for ln in stdout.getvalue().splitlines()]
    assert len(lines) == 3  # notification gets no reply
    assert [ln["id"] for ln in lines] == [1, 2, 3]
    assert lines[0]["result"]["serverInfo"]["name"] == "muse-cli"
