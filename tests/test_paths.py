"""Tests for muse_cli.paths."""
import os

import pytest

from muse_cli import paths


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # paths are computed at import from MUSE_HOME; re-derive for the test
    monkeypatch.setattr(paths, "MUSE_HOME", str(tmp_path / ".muse"))
    monkeypatch.setattr(paths, "STATUS_DIR", str(tmp_path / ".muse" / "status"))
    monkeypatch.setattr(paths, "IDENTITY_PATH",
                        str(tmp_path / ".muse" / "cli-identity.json"))
    monkeypatch.setattr(paths, "ALL_DIRS",
                        [str(tmp_path / ".muse" / d) for d in
                         ("queue", "results", "status")])
    return tmp_path


def test_status_dir_under_muse_home():
    assert paths.STATUS_DIR == os.path.join(paths.MUSE_HOME, "status")


def test_identity_path_under_muse_home():
    assert paths.IDENTITY_PATH == os.path.join(paths.MUSE_HOME, "cli-identity.json")


def test_status_dir_in_all_dirs():
    assert paths.STATUS_DIR in paths.ALL_DIRS


def test_ensure_dirs_creates_status(fake_home):
    paths.ensure_dirs()
    assert os.path.isdir(str(fake_home / ".muse" / "status"))


def test_ensure_dirs_idempotent(fake_home):
    paths.ensure_dirs()
    paths.ensure_dirs()  # must not raise
    assert os.path.isdir(str(fake_home / ".muse" / "status"))
