"""Tests for muse_cli.update (self-update helpers)."""
from muse_cli import update


class _Proc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_run(mapping):
    """mapping: tuple(git args after ['git', '-C', root]) -> (rc, stdout)."""
    def fake_run(cmd, **kwargs):
        rc, out = mapping.get(tuple(cmd[3:]), (1, ""))
        return _Proc(rc, out)
    return fake_run


LOCAL = "a" * 40
REMOTE = "b" * 40


def _check_mapping(remote_sha):
    return {
        ("rev-parse", "--abbrev-ref", "HEAD"): (0, "main\n"),
        ("rev-parse", "HEAD"): (0, LOCAL + "\n"),
        ("ls-remote", "origin", "main"):
            (0, f"{remote_sha}\trefs/heads/main\n"),
    }


def _git_dir(tmp_path):
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


def test_check_not_a_git_checkout(tmp_path):
    assert update.check(str(tmp_path)) is None


def test_check_up_to_date(tmp_path, monkeypatch):
    root = _git_dir(tmp_path)
    monkeypatch.setattr(update.subprocess, "run",
                        _fake_run(_check_mapping(LOCAL)))
    assert update.check(root) == {
        "behind": False, "local": LOCAL, "remote": LOCAL,
        "branch": "main"}


def test_check_behind(tmp_path, monkeypatch):
    root = _git_dir(tmp_path)
    monkeypatch.setattr(update.subprocess, "run",
                        _fake_run(_check_mapping(REMOTE)))
    st = update.check(root)
    assert st is not None
    assert st["behind"] is True
    assert st["local"] == LOCAL
    assert st["remote"] == REMOTE
    assert st["branch"] == "main"


def test_check_ls_remote_fails(tmp_path, monkeypatch):
    root = _git_dir(tmp_path)
    mapping = _check_mapping(REMOTE)
    mapping[("ls-remote", "origin", "main")] = (1, "")
    monkeypatch.setattr(update.subprocess, "run", _fake_run(mapping))
    assert update.check(root) is None


def test_pull_success(tmp_path, monkeypatch):
    root = _git_dir(tmp_path)
    monkeypatch.setattr(update.subprocess, "run",
                        lambda cmd, **kw: _Proc(0, "Already up to date.\n"))
    ok, _msg = update.pull(root)
    assert ok is True


def test_pull_fails_on_dirty_tree(tmp_path, monkeypatch):
    root = _git_dir(tmp_path)
    monkeypatch.setattr(
        update.subprocess, "run",
        lambda cmd, **kw: _Proc(
            1, "error: Your local changes would be overwritten\n"))
    ok, msg = update.pull(root)
    assert ok is False
    assert "local changes" in msg


def test_pull_not_a_checkout(tmp_path):
    ok, msg = update.pull(str(tmp_path))
    assert ok is False
    assert msg
