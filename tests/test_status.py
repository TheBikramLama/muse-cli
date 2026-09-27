"""Tests for the realtime agent-status polling on TaskCard.

Covers TaskCard._poll_agent_status without needing a running Textual app:
the method only touches self.rid, self._status_mtime, self._line,
self._step_text and self._render_live().
"""
import os
import time

import pytest

from muse_cli import paths
from muse_cli.app import TaskCard


@pytest.fixture
def status_dir(tmp_path, monkeypatch):
    d = tmp_path / "status"
    d.mkdir()
    monkeypatch.setattr(paths, "STATUS_DIR", str(d), raising=False)
    # app.py binds STATUS_DIR directly at import
    import muse_cli.app as app_mod
    monkeypatch.setattr(app_mod, "STATUS_DIR", str(d), raising=False)
    return d


def _card(rid="t1"):
    # Bypass __init__ (needs Textual widget machinery); the method under
    # test only uses these four attributes.
    c = TaskCard.__new__(TaskCard)
    c.rid = rid
    c._status_mtime = 0.0
    c._line = ""
    c._step_text = "old"
    c.rendered = []
    c._render_live = lambda: c.rendered.append(c._line)  # noqa: E731
    return c


def test_no_file_noop(status_dir):
    c = _card()
    c._poll_agent_status()
    assert c._line == "" and c.rendered == []


def test_latest_line_wins(status_dir):
    (status_dir / "t1.txt").write_text("first\nsecond\n")
    c = _card()
    c._poll_agent_status()
    assert c._line == "second"
    assert c._step_text == ""
    assert c.rendered == ["second"]


def test_mtime_guard_no_rerender(status_dir):
    (status_dir / "t1.txt").write_text("x\n")
    c = _card()
    c._poll_agent_status()
    c.rendered.clear()
    c._poll_agent_status()
    assert c.rendered == []


def test_append_picked_up(status_dir):
    p = status_dir / "t1.txt"
    p.write_text("one\n")
    c = _card()
    c._poll_agent_status()
    time.sleep(0.02)
    p.write_text("one\ntwo\n")
    # ensure mtime actually advances (coarse filesystems)
    os.utime(p, (time.time() + 1, time.time() + 1))
    c._poll_agent_status()
    assert c._line == "two"


def test_blank_lines_ignored(status_dir):
    (status_dir / "t1.txt").write_text("\n   \nreal\n\n")
    c = _card()
    c._poll_agent_status()
    assert c._line == "real"


def test_empty_file_keeps_old_line(status_dir):
    p = status_dir / "t1.txt"
    p.write_text("keep me\n")
    c = _card()
    c._poll_agent_status()
    assert c._line == "keep me"
    time.sleep(0.02)
    p.write_text("")
    os.utime(p, (time.time() + 1, time.time() + 1))
    c._poll_agent_status()
    assert c._line == "keep me"


def test_unicode_status(status_dir):
    (status_dir / "t1.txt").write_text("building \U0001f6a7 50%\n", encoding="utf-8")
    c = _card()
    c._poll_agent_status()
    assert c._line == "building \U0001f6a7 50%"
