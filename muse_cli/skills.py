"""Skill management: ~/.muse/skills/<name>/SKILL.md (Claude-compatible).

A skill is a directory whose root holds a SKILL.md with YAML frontmatter,
e.g.:

    ---
    name: my-skill
    description: Does useful things.
    ---

    # My Skill
    ...

Muse (the app) can reference skills by name in a task request's "skills"
field; the TUI shows them as badges on the task card and marks which
installed skills are actively used by running tasks.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess

from .paths import SKILLS_DIR, ensure_dirs

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SKILL_FILE = "SKILL.md"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def parse_frontmatter(text: str) -> dict:
    """Parse SKILL.md frontmatter without a YAML dependency.

    Reads the leading --- ... --- block as simple `key: value` lines.
    Anything fancier is ignored; description/name survive either way.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data: dict[str, str] = {}
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def skill_path(name: str) -> str:
    return os.path.join(SKILLS_DIR, name)


def list_skills() -> list[dict]:
    """Installed skills: {name, description, path}, sorted by name."""
    ensure_dirs()
    try:
        names = sorted(os.listdir(SKILLS_DIR))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        if not valid_name(name):
            continue
        md = os.path.join(skill_path(name), SKILL_FILE)
        if not os.path.isfile(md):
            continue
        desc = ""
        try:
            with open(md) as f:
                desc = parse_frontmatter(f.read()).get("description", "")
        except OSError:
            pass
        out.append({"name": name, "description": desc, "path": skill_path(name)})
    return out


def read_skill(name: str) -> str | None:
    """Raw SKILL.md content, or None when missing/invalid."""
    if not valid_name(name):
        return None
    try:
        with open(os.path.join(skill_path(name), SKILL_FILE)) as f:
            return f.read()
    except OSError:
        return None


def _dir_name(src: str) -> str:
    base = src.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[:-4]
    return base


def install_skill(src: str) -> tuple[bool, str]:
    """Install a skill from a git URL or a local path.

    Returns (ok, message). The source must contain SKILL.md at its root.
    """
    ensure_dirs()
    src = (src or "").strip()
    if not src:
        return False, "usage: /skill install <git-url|local-path>"
    is_url = "://" in src or src.startswith("git@")
    name = _dir_name(src)
    if not valid_name(name):
        return False, f"bad skill name derived from source: {name!r}"
    dest = skill_path(name)
    if os.path.exists(dest):
        return False, f"skill already installed: {name}"
    if is_url:
        try:
            p = subprocess.run(
                ["git", "clone", "--depth", "1", src, dest],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except Exception as e:
            return False, f"clone failed: {e}"
        if p.returncode != 0:
            return False, f"clone failed: {(p.stderr or p.stdout).strip()[:200]}"
    else:
        src_path = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(src_path):
            return False, f"not a directory: {src}"
        try:
            shutil.copytree(src_path, dest)
        except Exception as e:
            return False, f"copy failed: {e}"
    if not os.path.isfile(os.path.join(dest, SKILL_FILE)):
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"no {SKILL_FILE} at the skill root — not a skill"
    return True, f"installed skill: {name}"


def remove_skill(name: str) -> tuple[bool, str]:
    """Remove an installed skill. Returns (ok, message)."""
    name = (name or "").strip()
    if not valid_name(name):
        return False, f"bad skill name: {name!r}"
    dest = skill_path(name)
    if not os.path.isdir(dest):
        return False, f"no such skill: {name}"
    shutil.rmtree(dest, ignore_errors=True)
    return True, f"removed skill: {name}"
