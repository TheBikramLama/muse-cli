"""Tests for the synchronous bridge wrapper (muse_cli.exec_cmd)."""
import json
import os

import pytest

from muse_cli import exec_cmd
from muse_cli import paths as paths_mod


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    q = tmp_path / "queue"
    r = tmp_path / "results"
    c = tmp_path / "cancel"
    q.mkdir()
    r.mkdir()
    c.mkdir()
    monkeypatch.setattr(paths_mod, "QUEUE_DIR", str(q))
    monkeypatch.setattr(paths_mod, "RESULTS_DIR", str(r))
    monkeypatch.setattr(paths_mod, "CANCEL_DIR", str(c))
    return q, r, c


@pytest.fixture
def settings(monkeypatch):
    s = {"allowlist": ["echo", "git"],
         "default_timeout": 120, "max_timeout": 1500}
    monkeypatch.setattr(exec_cmd, "load_settings", lambda: s)
    return s


def _queue_files(q):
    return [n for n in os.listdir(str(q)) if n.endswith(".json")]


def _load_queued(q):
    names = _queue_files(q)
    assert len(names) == 1
    with open(os.path.join(str(q), names[0])) as f:
        return json.load(f)


def test_allowlist_rejection_exits_2(dirs, settings, capsys):
    q, r, c = dirs
    rc = exec_cmd.exec_main(["--", "rm", "-rf", "/"])
    assert rc == 2
    assert "not in the allowlist" in capsys.readouterr().err
    assert _queue_files(q) == []  # nothing queued


def test_empty_cmd_exits_2(dirs, settings, capsys):
    assert exec_cmd.exec_main([]) == 2
    assert exec_cmd.exec_main(["--"]) == 2


def test_bad_session_exits_2(dirs, settings, capsys):
    rc = exec_cmd.exec_main(["--session", "not a session!", "--", "echo", "hi"])
    assert rc == 2
    assert "invalid session" in capsys.readouterr().err


def test_queued_request_fields(dirs, settings, monkeypatch):
    q, r, c = dirs
    monkeypatch.setattr(exec_cmd, "NO_BRIDGE_S", 0)  # exit 3 fast
    rc = exec_cmd.exec_main(["--cwd", "/tmp", "--session", "work",
                             "--", "git", "status"])
    assert rc == 3
    req = _load_queued(q)
    assert req["cmd"] == ["git", "status"]
    assert req["cwd"] == "/tmp"
    assert req["session"] == "work"
    assert req["source"] == "muse-cli-exec"
    assert req["timeout"] == 120  # default_timeout
    assert req["id"]
    assert req["task"]


def test_timeout_clamped_to_max(dirs, settings, monkeypatch):
    q, r, c = dirs
    monkeypatch.setattr(exec_cmd, "NO_BRIDGE_S", 0)
    rc = exec_cmd.exec_main(["--timeout", "99999", "--", "echo", "hi"])
    assert rc == 3
    assert _load_queued(q)["timeout"] == 1500


def test_default_cwd_is_current_dir(dirs, settings, monkeypatch):
    q, r, c = dirs
    monkeypatch.setattr(exec_cmd, "NO_BRIDGE_S", 0)
    rc = exec_cmd.exec_main(["echo", "hi"])  # no --, unambiguous
    assert rc == 3
    assert _load_queued(q)["cwd"] == os.getcwd()


def test_result_arrives_mid_poll(dirs, settings, capsys, monkeypatch):
    q, r, c = dirs
    real_sleep = exec_cmd.time.sleep

    def fake_sleep(s):
        # Drop a result file the first time we would sleep: it must be
        # picked up on a later poll, printed, and deleted.
        names = _queue_files(q)
        rid = names[0][:-len(".json")]
        with open(os.path.join(str(r), rid + ".json"), "w") as f:
            json.dump({"id": rid, "ok": True, "exit": 3,
                       "stdout": "out\n", "stderr": "err\n"}, f)
        monkeypatch.setattr("time.sleep", real_sleep)

    monkeypatch.setattr("time.sleep", fake_sleep)
    rc = exec_cmd.exec_main(["--", "echo", "hi"])
    assert rc == 3  # command's exit code propagates
    out = capsys.readouterr()
    assert out.out == "out\n"
    assert out.err == "err\n"
    assert os.listdir(str(r)) == []  # result file consumed


def test_result_without_exit_code_exits_1(dirs, settings, capsys, monkeypatch):
    q, r, c = dirs
    rid = "deadbeef"
    with open(os.path.join(str(r), rid + ".json"), "w") as f:
        json.dump({"id": rid, "ok": False, "exit": None,
                   "stdout": "", "stderr": "boom\n"}, f)
    monkeypatch.setattr(exec_cmd, "new_id", lambda: rid)
    rc = exec_cmd.exec_main(["--", "echo", "hi"])
    assert rc == 1
    assert capsys.readouterr().err == "boom\n"


def test_timeout_drops_cancel_and_exits_124(dirs, settings, capsys):
    q, r, c = dirs
    rc = exec_cmd.exec_main(["--timeout", "1", "--", "echo", "hi"])
    assert rc == 124
    cancels = os.listdir(str(c))
    assert len(cancels) == 1
    rid = cancels[0]
    err = capsys.readouterr().err
    assert rid in err  # id printed for follow-up
    assert "timed out" in err


def test_no_bridge_exits_3_leaves_queue(dirs, settings, capsys, monkeypatch):
    q, r, c = dirs
    monkeypatch.setattr(exec_cmd, "NO_BRIDGE_S", 0)
    rc = exec_cmd.exec_main(["--", "echo", "hi"])
    assert rc == 3
    err = capsys.readouterr().err
    assert "no bridge" in err
    assert "./run.sh" in err
    assert len(_queue_files(q)) == 1  # left in place for a later bridge


def test_claim_marker_suppresses_no_bridge_exit(dirs, settings, capsys,
                                                monkeypatch):
    q, r, c = dirs
    monkeypatch.setattr(exec_cmd, "NO_BRIDGE_S", 0)
    monkeypatch.setattr(exec_cmd, "POLL_S", 0.01)
    rid = "claimed1"
    monkeypatch.setattr(exec_cmd, "new_id", lambda: rid)
    # A bridge claims the request but never finishes; with --timeout 1 we
    # must hit the 124 path, not the exit-3 no-bridge path.
    with open(os.path.join(str(q), rid + ".json.claimed.4242"), "w") as f:
        f.write("{}")
    rc = exec_cmd.exec_main(["--timeout", "1", "--", "echo", "hi"])
    assert rc == 124
    assert os.listdir(str(c)) == [rid]
