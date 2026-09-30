"""Tests for the agent activity report channel (muse_cli.activity)."""
import json
import os
import time

import pytest

from muse_cli import activity


@pytest.fixture
def act_home(tmp_path, monkeypatch):
    d = tmp_path / "activity"
    d.mkdir()
    monkeypatch.setattr(activity, "ACTIVITY_DIR", str(d))
    return d


def _load(act_home, aid):
    with open(os.path.join(str(act_home),
                           activity._safe_agent(aid) + ".json")) as f:
        return json.load(f)


def test_report_creates_and_updates(act_home):
    p = activity.report("side-chat:1", task="Fix drag", status="reading code",
                        label="side-chat · lipi")
    rec = _load(act_home, "side-chat:1")
    assert rec["task"] == "Fix drag"
    assert rec["status"] == "reading code"
    t0 = rec["t0"]
    time.sleep(0.01)
    activity.report("side-chat:1", status="editing file")
    rec2 = _load(act_home, "side-chat:1")
    assert rec2["t0"] == t0  # first-seen kept
    assert rec2["status"] == "editing file"
    assert rec2["at"] >= rec["at"]
    assert p.endswith("side-chat-1.json")


def test_report_sanitizes_agent_id(act_home):
    p = activity.report("../../evil", task="x")
    assert os.path.dirname(p) == str(act_home)
    assert "/" not in os.path.basename(p)


def test_read_all_flags_live_and_stale(act_home):
    activity.report("fresh", task="t", status="s")
    p = activity.report("old", task="t", status="s")
    with open(p) as f:
        rec = json.load(f)
    rec["at"] = time.time() - 300  # 5 min ago: stale but shown
    with open(p, "w") as f:
        json.dump(rec, f)
    p2 = activity.report("gone", task="t", status="s")
    with open(p2) as f:
        rec2 = json.load(f)
    rec2["at"] = time.time() - 20 * 60  # 20 min: dropped
    with open(p2, "w") as f:
        json.dump(rec2, f)
    out = activity.read_all()
    assert out["fresh"]["live"] is True
    assert out["old"]["live"] is False
    assert "gone" not in out
    assert out["fresh"]["elapsed"] >= 0


def test_clear_removes_report(act_home):
    activity.report("a1", task="t")
    activity.clear("a1")
    assert activity.read_all() == {}
    activity.clear("nonexistent")  # no crash


def test_todo_basename_stored(act_home):
    activity.report("a1", task="t", todo="/tmp/_auto_x.md")
    assert _load(act_home, "a1")["todo"] == "_auto_x.md"


def test_state_and_reason_persist(act_home):
    activity.report("a1", task="t", state="waiting", reason="need approval")
    rec = _load(act_home, "a1")
    assert rec["state"] == "waiting"
    assert rec["reason"] == "need approval"


def test_invalid_state_ignored(act_home):
    activity.report("a1", task="t", state="napping")
    assert "state" not in _load(act_home, "a1")


def _backdate(act_home, aid, minutes):
    p = os.path.join(str(act_home), activity._safe_agent(aid) + ".json")
    with open(p) as f:
        rec = json.load(f)
    rec["at"] = time.time() - minutes * 60
    with open(p, "w") as f:
        json.dump(rec, f)


def test_display_state_passthrough(act_home):
    activity.report("w", task="t", state="waiting", reason="r")
    activity.report("f", task="t", state="failed", reason="boom")
    out = activity.read_all()
    assert out["w"]["display_state"] == "waiting"
    assert out["f"]["display_state"] == "failed"


def test_working_quiet_not_yet_stalled(act_home):
    activity.report("q", task="t", state="working")
    _backdate(act_home, "q", 3)
    out = activity.read_all()
    assert out["q"]["display_state"] == "working"
    assert out["q"]["live"] is False


def test_working_quiet_auto_stalled(act_home):
    activity.report("s", task="t", state="working")
    _backdate(act_home, "s", 6)
    out = activity.read_all()
    assert out["s"]["display_state"] == "stalled"


def test_stateless_quiet_auto_stalled(act_home):
    activity.report("s2", task="t")
    _backdate(act_home, "s2", 6)
    out = activity.read_all()
    assert out["s2"]["display_state"] == "stalled"


def test_done_lingers_then_drops(act_home):
    activity.report("d", task="t")
    activity.mark_done("d")
    _backdate(act_home, "d", 4)
    out = activity.read_all()
    assert out["d"]["display_state"] == "done"
    _backdate(act_home, "d", 6)
    assert "d" not in activity.read_all()


def test_mark_done_refreshes_at_and_keeps_fields(act_home):
    activity.report("d2", task="Fix thing", label="my chat", state="working")
    _backdate(act_home, "d2", 10)
    activity.mark_done("d2", reason="shipped")
    rec = _load(act_home, "d2")
    assert rec["state"] == "done"
    assert rec["reason"] == "shipped"
    assert rec["task"] == "Fix thing"
    assert rec["label"] == "my chat"
    assert time.time() - rec["at"] < 60


def test_report_without_state_does_not_resurrect_done(act_home):
    activity.report("d3", task="t")
    activity.mark_done("d3")
    activity.report("d3", status="still here")
    rec = _load(act_home, "d3")
    assert rec["state"] == "done"
    assert rec["status"] == "still here"
