"""Tests for the v2 task todo lifecycle (muse_cli.auto_todo).

Covers: slugged creation from steps, single-command tasks, per-step
advancement, agent-edit preservation, terminal states (ok / cancelled /
failed), completed-list lingering then sweeping, abandonment sweeping
(halted agent / deleted side-chat), no-resurrection of deleted lists,
parallel-task isolation, manual item toggling, and the bridge end-to-end.
"""
import os
import time

import pytest

from muse_cli import auto_todo


@pytest.fixture
def todo_home(tmp_path, monkeypatch):
    d = tmp_path / "todos"
    d.mkdir()
    monkeypatch.setattr(auto_todo, "TODOS_DIR", str(d))
    return d


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _backdate(path, age_s):
    old = time.time() - age_s
    os.utime(path, (old, old))


def test_create_from_steps_uses_slug_name(todo_home):
    p = auto_todo.create("r1", "Widget Tests!", ["install", "run"], "bridge:main")
    assert os.path.basename(p) == "_auto_widget-tests-r1.md"
    text = _read(p)
    assert "# Widget Tests!" in text
    assert "<!-- auto:rid=r1 owner=bridge:main t0=" in text
    assert "- [ ] install" in text and "- [ ] run" in text


def test_create_single_command_uses_title(todo_home):
    p = auto_todo.create("r2", "echo hi", [], "bridge:main")
    assert "- [ ] echo hi" in _read(p)


def test_parse_roundtrip(todo_home):
    p = auto_todo.create("abc123", "My Task", ["a", "b"], "side-chat:9")
    info = auto_todo.parse(p)
    assert info["rid"] == "abc123"
    assert info["owner"] == "side-chat:9"
    assert info["title"] == "My Task"
    assert info["done_marker"] == ""
    assert len(info["items"]) == 2
    assert auto_todo.find_path("abc123") == p


def test_advance_checks_items_in_order(todo_home):
    p = auto_todo.create("r1", "t", ["a", "b", "c"], "o")
    auto_todo.advance(p, 1)
    text = _read(p)
    assert "- [x] a" in text and "- [ ] b" in text
    auto_todo.advance(p, 2)
    text = _read(p)
    assert "- [x] b" in text and "- [ ] c" in text


def test_advance_preserves_agent_edits_and_never_unchecks(todo_home):
    p = auto_todo.create("r1", "t", ["a", "b"], "o")
    with open(p, "a", encoding="utf-8") as f:
        f.write("- [ ] agent-added detail\nsome freeform note\n")
    auto_todo.advance(p, 2)
    text = _read(p)
    assert "- [x] a" in text and "- [x] b" in text
    assert "- [ ] agent-added detail" in text
    assert "some freeform note" in text
    # Manual toggle survives a later bridge advance.
    auto_todo.toggle_item(p, 2)
    auto_todo.advance(p, 2)
    assert "- [x] agent-added detail" in _read(p)


def test_advance_never_resurrects_deleted(todo_home):
    p = auto_todo.create("r1", "t", ["a"], "o")
    os.remove(p)
    auto_todo.advance(p, 1)
    auto_todo.finish(p, True)
    assert not os.path.exists(p)


def test_toggle_item_flips(todo_home):
    p = auto_todo.create("r1", "t", ["a", "b"], "o")
    assert auto_todo.toggle_item(p, 0) is True
    assert "- [x] a" in _read(p)
    assert auto_todo.toggle_item(p, 0) is False
    assert "- [ ] a" in _read(p)
    assert auto_todo.toggle_item(p, 9) is None
    assert auto_todo.toggle_item("/nonexistent.md", 0) is None


def test_finish_ok_checks_all_and_marks(todo_home):
    p = auto_todo.create("r1", "t", ["a", "b"], "o")
    auto_todo.advance(p, 1)
    auto_todo.finish(p, True)
    text = _read(p)
    assert "- [x] a" in text and "- [x] b" in text
    assert "<!-- auto:done=ok -->" in text


def test_finish_failed_keeps_partial_without_marker(todo_home):
    p = auto_todo.create("r1", "t", ["a", "b", "c"], "o")
    auto_todo.advance(p, 1)
    auto_todo.finish(p, False, "boom at step 2")
    text = _read(p)
    assert "- [x] a" in text and "- [ ] b" in text and "- [ ] c" in text
    assert "auto:done=" not in text
    assert "boom at step 2" in text


def test_finish_cancelled_is_terminal(todo_home):
    p = auto_todo.create("r1", "t", ["a"], "o")
    auto_todo.finish(p, False, "cancelled")
    assert "<!-- auto:done=cancelled -->" in _read(p)


def test_completed_lingers_then_sweeps(todo_home):
    p = auto_todo.create("ra", "t", ["a"], "o")
    auto_todo.finish(p, True)
    auto_todo.create("rb", "t", ["a"], "o")  # next task: 4/4 still visible
    assert os.path.exists(p)
    _backdate(p, 6 * 60)  # 6 minutes later...
    assert auto_todo.sweep_completed() == 1
    assert not os.path.exists(p)


def test_sweep_abandoned_deletes_stale_incomplete(todo_home):
    p = auto_todo.create("gone", "t", ["a", "b"], "side-chat:gone")
    auto_todo.advance(p, 1)  # 1/2, owner vanishes
    _backdate(p, 31 * 60)
    res = auto_todo.sweep_abandoned()
    assert os.path.basename(p) in res["deleted"]
    assert not os.path.exists(p)


def test_sweep_abandoned_keeps_fresh(todo_home):
    p = auto_todo.create("live", "t", ["a", "b"], "side-chat:live")
    auto_todo.advance(p, 1)
    res = auto_todo.sweep_abandoned()
    assert res == {"deleted": [], "completed": []}
    assert os.path.exists(p)


def test_sweep_abandoned_completes_forgotten_done(todo_home):
    p = auto_todo.create("forgetful", "t", ["a"], "side-chat:x")
    auto_todo.toggle_item(p, 0)  # all checked, no done marker
    _backdate(p, 31 * 60)
    res = auto_todo.sweep_abandoned()
    assert os.path.basename(p) in res["completed"]
    assert "<!-- auto:done=ok -->" in _read(p)


def test_sweep_orphans_is_startup_abandonment_sweep(todo_home):
    p1 = auto_todo.create("crashed", "t", ["a", "b"], "o")
    auto_todo.advance(p1, 1)
    _backdate(p1, 31 * 60)
    p2 = auto_todo.create("fresh", "t", ["a", "b"], "o")
    auto_todo.advance(p2, 1)  # live task: fresh mtime
    auto_todo.sweep_orphans()
    assert not os.path.exists(p1)
    assert os.path.exists(p2)


def test_sweep_abandoned_skips_live_rids(todo_home):
    p = auto_todo.create("running", "t", ["a", "b"], "bridge:main")
    auto_todo.advance(p, 1)
    _backdate(p, 31 * 60)  # quiet for 31 min, but still running
    res = auto_todo.sweep_abandoned(live_rids={"running"})
    assert res == {"deleted": [], "completed": []}
    assert os.path.exists(p)
    # Same file without the skip-list: swept.
    res = auto_todo.sweep_abandoned()
    assert os.path.basename(p) in res["deleted"]


def test_manual_lists_never_swept(todo_home):
    mine = os.path.join(str(todo_home), "mine.md")
    with open(mine, "w") as f:
        f.write("- [x] done\n")
    _backdate(mine, 60 * 60)
    auto_todo.sweep_abandoned()
    auto_todo.sweep_completed()
    assert os.path.exists(mine)


def test_parallel_tasks_are_isolated(todo_home):
    p1 = auto_todo.create("p1", "Same Title", ["a", "b"], "o")
    p2 = auto_todo.create("p2", "Same Title", ["x", "y"], "o")
    assert p1 != p2  # same slug, distinct ids
    auto_todo.advance(p1, 2)
    t2 = _read(p2)
    assert "- [ ] x" in t2 and "- [ ] y" in t2
    auto_todo.finish(p1, True)
    assert os.path.exists(p2)


def test_mark_done_idempotent(todo_home):
    p = auto_todo.create("r1", "t", ["a"], "o")
    auto_todo.mark_done(p)
    auto_todo.mark_done(p)
    assert _read(p).count("auto:done=ok") == 1


def test_bridge_run_one_drives_full_lifecycle(todo_home, tmp_path,
                                              monkeypatch):
    """End-to-end through Bridge._run_one: create -> advance -> finish(ok)."""
    import json
    from muse_cli import bridge as _bridge

    results = {}
    monkeypatch.setattr(_bridge, "write_result",
                        lambda res: results.update(json.loads(json.dumps(res))))
    settings = {
        "allowlist": ["echo"],
        "allowed_roots": [],
        "default_timeout": 30,
        "max_timeout": 60,
        "max_output_bytes": 65536,
        "auto_approve": True,
    }
    b = _bridge.Bridge(settings, session="main")
    req = {
        "id": "e2e1",
        "task": "demo run",
        "steps": [
            {"name": "say hi", "cmd": ["echo", "hi"]},
            {"cmd": ["echo", "yo"]},  # no name -> command becomes the label
        ],
        "session": "main",
    }
    saw_midrun = {}
    orig_advance = auto_todo.advance

    def spy_advance(path, done):
        if path and os.path.exists(path):
            saw_midrun["text"] = _read(path)
        return orig_advance(path, done)

    monkeypatch.setattr(auto_todo, "advance", spy_advance)
    b._run_one(req, os.path.join(str(tmp_path), "claimed"))
    assert results.get("ok") is True
    p = auto_todo.find_path("e2e1")
    assert p is not None
    text = _read(p)
    assert "- [x] say hi" in text and "- [x] echo yo" in text
    assert "<!-- auto:done=ok -->" in text
    assert "bridge:main" in text
    # The bridge really did advance mid-run (not just at the end).
    assert saw_midrun.get("text") is not None
