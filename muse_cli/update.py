"""Self-update helpers for muse-cli.

The CLI is distributed as a git checkout; updates land as commits on the
tracked branch. These helpers compare the local HEAD against the remote
without changing any local state (git ls-remote, no fetch).

Everything here fails safe: no git, not a checkout, or no network just
returns None / (False, reason) instead of raising.
"""
from __future__ import annotations

import os
import subprocess

#: ls-remote must not hang forever; it runs on a worker thread anyway.
_CHECK_TIMEOUT_S = 20
_PULL_TIMEOUT_S = 120


def repo_root() -> str | None:
    """Root of the checkout this code is running from, or None."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isdir(os.path.join(root, ".git")):
        return root
    return None


def _git(root: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True, text=True, timeout=_CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def check(root: str | None = None) -> dict | None:
    """Compare local HEAD with the remote tracking branch.

    Returns None when the check cannot run (not a git checkout, no git,
    no network). Otherwise {"behind": bool, "local": sha, "remote": sha,
    "branch": name}.
    """
    root = root or repo_root()
    if root is None:
        return None
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    local = _git(root, "rev-parse", "HEAD")
    if not branch or not local:
        return None
    out = _git(root, "ls-remote", "origin", branch)
    if not out:
        return None
    remote = out.split()[0]
    return {"behind": remote != local, "local": local,
            "remote": remote, "branch": branch}


def pull(root: str | None = None) -> tuple[bool, str]:
    """Fast-forward the checkout to the remote. Returns (ok, message)."""
    root = root or repo_root()
    if root is None:
        return False, "not a git checkout"
    try:
        proc = subprocess.run(
            ["git", "-C", root, "pull", "--ff-only"],
            capture_output=True, text=True, timeout=_PULL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = (proc.stdout.strip() + "\n" + proc.stderr.strip()).strip()
    if proc.returncode != 0:
        return False, out or "git pull failed"
    return True, out or "already up to date"
