"""System-managed todo lists for bridge tasks — maximum visibility.

The bridge auto-creates ``~/.muse/todos/_auto_<rid>.md`` when a task starts
(from its ``steps``, or a single item for a plain command), checks items off
as steps complete, and records the terminal state. Agents update the same
file for finer-grained progress (see PROTOCOL.md) — it is the user's
realtime view of the work.

Lifecycle rules (kept deliberately simple and truthful):
- Success: every item checked; the ``4/4`` list lingers until the next task
  starts, then it is swept. Visibility without unbounded accumulation.
- Failure / cancellation: items stay exactly as they were (partial progress
  is honest); the list lingers until ``/todo clear`` — failures deserve
  attention, not silent cleanup.
- Crash orphans (no terminal marker, task never finished) are swept at
  bridge startup so a restart never shows a stale ``2/4``.
- Parallel tasks each get their own file (``<rid>`` is unique); advancing
  one never touches another.
- If the file was deleted (``/todo clear`` or the agent cleaned up), the
  bridge never resurrects it.
"""
from __future__ import annotations

import os
import re

from .paths import TODOS_DIR, ensure_dirs

AUTO_PREFIX = "_auto_"
AUTO_SUFFIX = ".md"
DONE_MARKER = "<!-- auto:done=%s -->"  # %s: ok | failed | cancelled


def auto_name(rid: str) -> str:
    return f"{AUTO_PREFIX}{rid}{AUTO_SUFFIX}"


def auto_path(rid: str) -> str:
    return os.path.join(TODOS_DIR, auto_name(rid))


def is_auto(name: str) -> bool:
    return (name.startswith(AUTO_PREFIX) and name.endswith(AUTO_SUFFIX)
            and len(name) > len(AUTO_PREFIX) + len(AUTO_SUFFIX))


_ITEM_RE = re.compile(r"^(\s*[-*]\s+)\[( |x|X)\](.*)$")


def _read_items(path: str) -> list[tuple[str, bool, str]] | None:
    """Parse checklist lines as (prefix, checked, rest); None if unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    items = []
    for ln in lines:
        m = _ITEM_RE.match(ln)
        if m:
            items.append((m.group(1), m.group(2).lower() == "x", m.group(3)))
    return items


def _write_items(path: str, items: list[tuple[str, bool, str]],
                 header: list[str]) -> None:
    lines = list(header)
    for prefix, checked, rest in items:
        lines.append(f"{prefix}[{'x' if checked else ' '}]{rest}")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def _has_marker(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as f:
            return "<!-- auto:done=" in f.read()
    except OSError:
        return False


def create(rid: str, title: str, steps: list[str]) -> None:
    """Create the auto todo list for a task; sweep older completed ones."""
    ensure_dirs()
    title = (title or rid).strip() or rid
    items = steps if steps else [title]
    header = [f"# {title}", f"<!-- auto:rid={rid} -->"]
    _write_items(auto_path(rid),
                 [("- ", False, f" {s}") for s in items], header)
    sweep_completed(except_rid=rid)


def advance(rid: str, done: int) -> None:
    """Check the first ``done`` items. Never resurrects a deleted file and
    never unchecks or renames anything the agent wrote."""
    path = auto_path(rid)
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return  # deleted by /todo clear or the agent — stay deleted
    lines = text.splitlines()
    seen = 0
    out = []
    for ln in lines:
        m = _ITEM_RE.match(ln)
        if m and seen < done:
            seen += 1
            out.append(f"{m.group(1)}[x]{m.group(3)}")
        else:
            out.append(ln)
    if seen == 0:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError:
        pass


def finish(rid: str, ok: bool, note: str = "") -> None:
    """Record the terminal state. Success checks everything off; failure or
    cancellation leaves partial progress exactly as it was. Both get a
    marker so startup can tell them apart from crash orphans."""
    path = auto_path(rid)
    items = _read_items(path)
    if items is None:
        return
    marker = DONE_MARKER % ("ok" if ok else ("cancelled" if note == "cancelled"
                                            else "failed"))
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return
    lines = text.splitlines()
    if ok:
        lines = [(_ITEM_RE.sub(lambda m: f"{m.group(1)}[x]{m.group(3)}", ln)
                  if _ITEM_RE.match(ln) else ln) for ln in lines]
    if note and not ok:
        lines.append(f"> {note[:120]}")
    lines.append(marker)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def sweep_completed(except_rid: str | None = None) -> None:
    """Delete fully-checked auto lists (their 4/4 had its moment). Failed or
    partial lists are kept — they need attention, use /todo clear."""
    try:
        names = os.listdir(TODOS_DIR)
    except OSError:
        return
    for name in names:
        if not is_auto(name):
            continue
        if except_rid and name == auto_name(except_rid):
            continue
        items = _read_items(os.path.join(TODOS_DIR, name))
        if items and all(c for _, c, _ in items):
            try:
                os.remove(os.path.join(TODOS_DIR, name))
            except OSError:
                pass


def sweep_orphans() -> None:
    """At bridge startup: drop auto lists from tasks that died without a
    terminal marker. Marked (finished) lists linger truthfully."""
    try:
        names = os.listdir(TODOS_DIR)
    except OSError:
        return
    for name in names:
        if not is_auto(name):
            continue
        path = os.path.join(TODOS_DIR, name)
        if _has_marker(path):
            continue
        items = _read_items(path)
        if items is None:
            continue
        if any(not c for _, c, _ in items):
            try:
                os.remove(path)
            except OSError:
                pass
