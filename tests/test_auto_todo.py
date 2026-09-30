"""Tests for the automatic task todo lifecycle (muse_cli.auto_todo).

Covers: creation from steps, single-command tasks, per-step advancement,
agent-edit preservation, terminal states (ok / failed / cancelled),
completed-list sweeping on the next task, crash-orphan sweeping at startup,
parallel-task isolation, and no-resurrection of deleted lists.
"""
import os

import pytest

from muse_cli import auto_todo


@pytest.fixture
def todo_home(tmp_path, monkeypatch):
    d = tmp_path / "todos"
    d.mkdir()
    monkeypatch.setattr(auto_todo, "TODOS_DIR", str(d))
    return d


def _read(name, todo_home):
    with open(os.path.join(str(todo_home), name), encoding="utf-8") as f:
        return f.read()


def test_create_from_steps(todo_home):
    auto_todo.create("r1", "widget tests", ["install", "run", "report"])
    text = _read("_auto_r1.md", todo_home)
    assert "# widget tests" in text
    assert "- [ ] install" in text
    assert "- [ ] run" in text
    assert "- [ ] report" in text


def test_create_single_command_uses_title(todo_home):
    auto_todo.create("r2", "echo hi", [])
    text = _read("_auto_r2.md", todo_home)
    assert "- [ ] echo hi" in text


def test_advance_checks_items_in_order(todo_home):
    auto_todo.create("r1", "t", ["a", "b", "c"])
    auto_todo.advance("r1", 1)
    text = _read("_auto_r1.md", todo_home)
    assert "- [x] a" in text and "- [ ] b" in text
    auto_todo.advance("r1", 2)
    text = _read("_auto_r1.md", todo_home)
    assert "- [x] b" in text and "- [ ] c" in text


def test_advance_preserves_agent_edits(todo_home):
    auto_todo.create("r1", "t", ["a", "b"])
    p = os.path.join(str(todo_home), "_auto_r1.md")
    with open(p, "a", encoding="utf-8") as f:
        f.write("- [ ] agent-added detail\n")
        f.write("some freeform note\n")
    auto_todo.advance("r1", 2)
    text = _read("_auto_r1.md", todo_home)
    assert "- [x] a" in text and "- [x] b" in text
    assert "- [ ] agent-added detail" in text
    assert "some freeform note" in text


def test_advance_never_resurrects_deleted(todo_home):
    auto_todo.create("r1", "t", ["a"])
    os.remove(os.path.join(str(todo_home), "_auto_r1.md"))
    auto_todo.advance("r1", 1)
    auto_todo.finish("r1", True)
    assert not os.path.exists(os.path.join(str(todo_home), "_auto_r1.md"))


def test_finish_ok_checks_all_and_marks(todo_home):
    auto_todo.create("r1", "t", ["a", "b"])
    auto_todo.advance("r1", 1)
    auto_todo.finish("r1", True)
    text = _read("_auto_r1.md", todo_home)
    assert "- [x] a" in text and "- [x] b" in text
    assert "<!-- auto:done=ok -->" in text


def test_finish_failed_keeps_partial_and_marks(todo_home):
    auto_todo.create("r1", "t", ["a", "b", "c"])
    auto_todo.advance("r1", 1)
    auto_todo.finish("r1", False, "boom at step 2")
    text = _read("_auto_r1.md", todo_home)
    assert "- [x] a" in text and "- [ ] b" in text and "- [ ] c" in text
    assert "<!-- auto:done=failed -->" in text
    assert "boom at step 2" in text


def test_finish_cancelled_marks_cancelled(todo_home):
    auto_todo.create("r1", "t", ["a"])
    auto_todo.finish("r1", False, "cancelled")
    assert "<!-- auto:done=cancelled -->" in _read("_auto_r1.md", todo_home)


def test_next_task_sweeps_completed_but_keeps_failed(todo_home):
    auto_todo.create("ra", "t", ["a"])
    auto_todo.finish("ra", True)          # 1/1 -> lingers...
    auto_todo.create("rb", "t", ["a"])    # ...until the next task starts
    auto_todo.finish("rb", False, "nope")  # failed -> lingers
    auto_todo.create("rc", "t", ["a"])
    names = set(os.listdir(str(todo_home)))
    assert "_auto_ra.md" not in names      # completed: swept
    assert "_auto_rb.md" in names          # failed: kept for attention
    assert "_auto_rc.md" in names
    # Manual lists are never swept.
    with open(os.path.join(str(todo_home), "mine.md"), "w") as f:
        f.write("- [x] done\n")
    auto_todo.create("rd", "t", ["a"])
    assert os.path.exists(os.path.join(str(todo_home), "mine.md"))


def test_sweep_orphans_removes_crashed_but_keeps_finished(todo_home):
    # NB: create() sweeps completed lists, so set up all three first and
    # only then finish them — otherwise the completed-sweep interferes.
    auto_todo.create("crashed", "t", ["a", "b"])
    auto_todo.create("done", "t", ["a"])
    auto_todo.create("failed", "t", ["a"])
    auto_todo.advance("crashed", 1)        # 1/2, no terminal marker
    auto_todo.finish("done", True)         # marked ok
    auto_todo.finish("failed", False, "x")  # marked failed
    auto_todo.sweep_orphans()
    names = set(os.listdir(str(todo_home)))
    assert "_auto_crashed.md" not in names
    assert "_auto_done.md" in names
    assert "_auto_failed.md" in names


def test_parallel_tasks_are_isolated(todo_home):
    auto_todo.create("p1", "t", ["a", "b"])
    auto_todo.create("p2", "t", ["x", "y", "z"])
    auto_todo.advance("p1", 2)
    t1 = _read("_auto_p1.md", todo_home)
    t2 = _read("_auto_p2.md", todo_home)
    assert "- [x] a" in t1 and "- [x] b" in t1
    assert "- [ ] x" in t2 and "- [ ] y" in t2 and "- [ ] z" in t2
    auto_todo.finish("p1", True)
    assert os.path.exists(os.path.join(str(todo_home), "_auto_p2.md"))


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
            {"name": "say yo", "cmd": ["echo", "yo"]},
        ],
        "session": "main",
    }
    p = os.path.join(str(todo_home), "_auto_e2e1.md")
    saw_midrun = {}

    orig_advance = auto_todo.advance

    def spy_advance(rid, done):
        if rid == "e2e1" and os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                saw_midrun["text"] = f.read()
        return orig_advance(rid, done)

    monkeypatch.setattr(auto_todo, "advance", spy_advance)
    b._run_one(req, os.path.join(str(tmp_path), "claimed"))
    assert results.get("ok") is True
    text = _read("_auto_e2e1.md", todo_home)
    assert "- [x] say hi" in text and "- [x] say yo" in text
    assert "<!-- auto:done=ok -->" in text
    # The bridge really did advance mid-run (not just at the end).
    assert saw_midrun.get("text") is not None
