"""Tests for muse_cli.setup: setup/doctor/pair/unpair."""
import json
import os
import re
import time

import pytest

from muse_cli import pairing, paths, setup, update


@pytest.fixture(autouse=True)
def _stub_update_check(monkeypatch):
    # update.check() hits the network (git ls-remote) against the real
    # checkout; stub it so doctor tests stay hermetic and fast.
    monkeypatch.setattr(
        update, "check",
        lambda root=None: {"behind": False, "local": "a" * 40,
                           "remote": "a" * 40, "branch": "main"})


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    mapping = {
        "MUSE_HOME": str(home / ".muse"),
        "STATUS_DIR": str(home / ".muse" / "status"),
        "IDENTITY_PATH": str(home / ".muse" / "cli-identity.json"),
        "SETTINGS_PATH": str(home / ".muse" / "settings.json"),
        "WATCHER_JSON": str(home / ".muse" / "watcher.json"),
        "PAIRING_DIR": str(home / ".muse" / "pairing"),
        "PAIRING_REQUEST_PATH": str(home / ".muse" / "pairing" / "request.json"),
        "PAIRING_RECEIPT_PATH": str(home / ".muse" / "pairing" / "receipt.json"),
        "PAIRED_PATH": str(home / ".muse" / "paired.json"),
        "ALL_DIRS": [str(home / ".muse" / d) for d in
                     ("queue", "results", "status")],
    }
    for attr, val in mapping.items():
        monkeypatch.setattr(paths, attr, val, raising=False)
        # setup.py binds these names directly at import
        monkeypatch.setattr(setup, attr, val, raising=False)
    return home


def _read_identity(home):
    with open(home / ".muse" / "cli-identity.json", encoding="utf-8") as f:
        return json.load(f)


def _read_request(home):
    with open(home / ".muse" / "pairing" / "request.json",
              encoding="utf-8") as f:
        return json.load(f)


def _pair(home):
    """Complete a pairing the way the Muse app would."""
    req = _read_request(home)
    with open(home / ".muse" / "pairing" / "receipt.json", "w") as f:
        json.dump({"code": req["code"], "nonce": req["nonce"],
                   "muse": "test-muse", "at": int(time.time())}, f)
    rec = pairing.check_receipt()
    assert rec is not None
    assert pairing.is_paired()


def test_setup_creates_dirs_and_identity(fake_home, capsys):
    assert setup.cmd_setup() == 0
    assert os.path.isdir(fake_home / ".muse" / "status")
    assert os.path.isdir(fake_home / ".muse" / "pairing")
    ident = _read_identity(fake_home)
    assert re.fullmatch(r"[0-9a-f]{32}", ident["cli_id"]), ident["cli_id"]
    req = _read_request(fake_home)
    out = capsys.readouterr().out
    assert req["code"] in out
    assert "PROTOCOL.md" in out


def test_setup_idempotent_keeps_identity(fake_home, capsys):
    assert setup.cmd_setup() == 0
    id1 = _read_identity(fake_home)["cli_id"]
    capsys.readouterr()
    assert setup.cmd_setup() == 0
    id2 = _read_identity(fake_home)["cli_id"]
    assert id1 == id2
    assert "CLI identity" in capsys.readouterr().out


def test_setup_rejects_old_python(fake_home, monkeypatch, capsys):
    monkeypatch.setattr(setup, "_python_ok", lambda: (False, "3.9.0"))
    assert setup.cmd_setup() == 1
    assert "3.10" in capsys.readouterr().out


def test_python_ok_current():
    ok_py, py = setup._python_ok()
    assert ok_py is True
    assert py.startswith("3.")


def test_doctor_reports_problems_fresh(fake_home, capsys):
    # No setup run: missing identity -> problems
    rc = setup.cmd_doctor()
    assert rc == 1
    out = capsys.readouterr().out
    assert "no CLI identity" in out


def test_doctor_after_setup(fake_home, capsys):
    assert setup.cmd_setup() == 0
    _pair(fake_home)
    capsys.readouterr()
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    # TUI not running in tests -> exactly 1 problem
    assert rc == 1
    assert "TUI not running" in out
    assert "inactive" in out  # Muse link never seen
    assert "paired with test-muse" in out


def test_doctor_update_available_is_a_problem(fake_home, capsys,
                                               monkeypatch):
    monkeypatch.setattr(
        update, "check",
        lambda root=None: {"behind": True, "local": "a" * 40,
                           "remote": "b" * 40, "branch": "main"})
    assert setup.cmd_setup() == 0
    _pair(fake_home)
    capsys.readouterr()
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "update available" in out


def test_doctor_unpaired_is_a_problem(fake_home, capsys):
    assert setup.cmd_setup() == 0
    capsys.readouterr()
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "not paired" in out


def test_doctor_active_watcher(fake_home, capsys, monkeypatch):
    assert setup.cmd_setup() == 0
    _pair(fake_home)
    capsys.readouterr()
    watcher = {"at": time.time(), "ok": True, "state": "idle",
               "mid": None, "error": None}
    with open(fake_home / ".muse" / "watcher.json", "w") as f:
        json.dump(watcher, f)
    # Fake a running TUI via our own pid
    pid_path = fake_home / ".muse" / "muse-cli.pid"
    pid_path.write_text(str(os.getpid()))
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "active" in out
    assert "TUI running" in out


def test_doctor_stale_watcher(fake_home, capsys):
    assert setup.cmd_setup() == 0
    capsys.readouterr()
    watcher = {"at": time.time() - 9999, "ok": True, "state": "idle",
               "mid": None, "error": None}
    with open(fake_home / ".muse" / "watcher.json", "w") as f:
        json.dump(watcher, f)
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "silent" in out


def test_doctor_error_watcher(fake_home, capsys):
    assert setup.cmd_setup() == 0
    capsys.readouterr()
    watcher = {"at": time.time(), "ok": False, "state": "idle",
               "mid": None, "error": "boom"}
    with open(fake_home / ".muse" / "watcher.json", "w") as f:
        json.dump(watcher, f)
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    assert rc == 1
    assert "boom" in out


def test_pair_prints_prompt_when_unpaired(fake_home, capsys, monkeypatch):
    # _wait_for_pairing would block: fail loudly if it is reached
    monkeypatch.setattr(setup, "_wait_for_pairing",
                        lambda: (_ for _ in ()).throw(AssertionError("blocked")))
    assert setup.cmd_setup() == 0
    capsys.readouterr()
    with pytest.raises(AssertionError):
        setup.cmd_pair()
    out = capsys.readouterr().out
    assert "Pairing code" in out
    assert "pairing/receipt.json" in out


def test_pair_already_paired(fake_home, capsys):
    assert setup.cmd_setup() == 0
    _pair(fake_home)
    capsys.readouterr()
    assert setup.cmd_pair() == 0
    assert "Already paired" in capsys.readouterr().out


def test_unpair_resets(fake_home, capsys):
    assert setup.cmd_setup() == 0
    _pair(fake_home)
    assert pairing.is_paired()
    capsys.readouterr()
    assert setup.cmd_unpair() == 0
    assert "Unpaired" in capsys.readouterr().out
    assert not pairing.is_paired()
    assert setup.load_identity() is None


def _write_pid(home, content: str):
    p = home / ".muse" / "muse-cli.pid"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_tui_running_json_pid_file(fake_home):
    # the TUI writes a JSON guard: {"pid": ..., "mode": "tui", ...}
    _write_pid(fake_home, json.dumps(
        {"pid": os.getpid(), "mode": "tui", "session": "main"}))
    assert setup._tui_running() is True


def test_tui_running_legacy_plain_pid(fake_home):
    _write_pid(fake_home, str(os.getpid()))
    assert setup._tui_running() is True


def test_tui_running_stale_pid(fake_home):
    _write_pid(fake_home, json.dumps({"pid": 2 ** 31 - 1,
                                      "mode": "tui"}))
    assert setup._tui_running() is False


def test_tui_running_missing_file(fake_home):
    assert setup._tui_running() is False


def test_tui_running_garbage(fake_home):
    _write_pid(fake_home, "not-json-or-a-pid")
    assert setup._tui_running() is False
