"""Tests for muse_cli.setup: `muse-cli setup` and `muse-cli doctor`."""
import json
import os
import re
import time

import pytest

from muse_cli import paths, setup


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


def test_setup_creates_dirs_and_identity(fake_home, capsys):
    assert setup.cmd_setup() == 0
    assert os.path.isdir(fake_home / ".muse" / "status")
    ident = _read_identity(fake_home)
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", ident["pairing_code"]), \
        ident["pairing_code"]
    assert len(ident["cli_id"]) == 32
    out = capsys.readouterr().out
    assert ident["pairing_code"] in out
    assert "PROTOCOL.md" in out


def test_setup_idempotent_keeps_pairing_code(fake_home, capsys):
    assert setup.cmd_setup() == 0
    code1 = _read_identity(fake_home)["pairing_code"]
    assert setup.cmd_setup() == 0
    code2 = _read_identity(fake_home)["pairing_code"]
    assert code1 == code2
    assert "kept" in capsys.readouterr().out


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
    capsys.readouterr()
    rc = setup.cmd_doctor()
    out = capsys.readouterr().out
    # TUI not running in tests -> exactly 1 problem
    assert rc == 1
    assert "TUI not running" in out
    assert "inactive" in out  # Muse link never seen


def test_doctor_active_watcher(fake_home, capsys, monkeypatch):
    assert setup.cmd_setup() == 0
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
