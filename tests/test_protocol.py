"""Tests for muse_cli.protocol queue/result helpers."""
import json
import os

import pytest

from muse_cli import paths, protocol


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    for name in ("QUEUE_DIR", "RESULTS_DIR", "CANCEL_DIR"):
        monkeypatch.setattr(paths, name, str(home / ".muse" / name.lower().replace("_dir", "")))
    # protocol imports the names directly; patch there too
    monkeypatch.setattr(protocol, "QUEUE_DIR", str(home / ".muse" / "queue"))
    monkeypatch.setattr(protocol, "RESULTS_DIR", str(home / ".muse" / "results"))
    monkeypatch.setattr(protocol, "CANCEL_DIR", str(home / ".muse" / "cancel"))
    monkeypatch.setattr("muse_cli.paths.ALL_DIRS",
                        [str(home / ".muse" / d) for d in ("queue", "results", "cancel")])
    return home


def test_submit_and_read_result(fake_home):
    rid = protocol.submit("Do a thing", cmd=["echo", "hi"], cwd="/tmp",
                          timeout=30, source="muse", session="main")
    qf = fake_home / ".muse" / "queue" / (rid + ".json")
    assert qf.exists()
    req = json.loads(qf.read_text())
    assert req["task"] == "Do a thing"
    assert req["cmd"] == ["echo", "hi"]
    assert req["session"] == "main"

    assert protocol.read_result(rid) is None
    protocol.write_result({"id": rid, "ok": True, "summary": "did it"})
    assert protocol.read_result(rid)["summary"] == "did it"


def test_take_result_removes_file(fake_home):
    rid = protocol.submit("x")
    protocol.write_result({"id": rid, "ok": True})
    assert protocol.take_result(rid)["ok"] is True
    assert protocol.read_result(rid) is None
    assert protocol.take_result(rid) is None


def test_cancel_touches_file(fake_home):
    protocol.cancel("abc123")
    assert (fake_home / ".muse" / "cancel" / "abc123").exists()


def test_submit_steps(fake_home):
    rid = protocol.submit("multi", steps=[{"name": "a", "cmd": ["true"]}])
    req = json.loads((fake_home / ".muse" / "queue" / (rid + ".json")).read_text())
    assert req["steps"] == [{"name": "a", "cmd": ["true"]}]


def test_torn_result_returns_none(fake_home):
    rid = protocol.submit("x")
    rp = fake_home / ".muse" / "results" / (rid + ".json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("{not json")
    assert protocol.read_result(rid) is None
