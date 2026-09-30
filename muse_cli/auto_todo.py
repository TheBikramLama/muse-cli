"""System-managed todo lists — the mission-control checklist.

The bridge auto-creates ``~/.muse/todos/_auto_<slug>-<id>.md`` when a task
starts (from its ``steps``, or a single item for a plain command), checks
items off as steps complete, and records the terminal state. Agents
(side-chats, subagents) create and update their own files under the same
convention — it is the user's realtime view of who is doing what.

File layout::

    <!-- auto:rid=<task-id> owner=<owner-id> t0=<epoch> -->
    # Human-readable task title
    - [x] Real step label
    - [ ] Another real step

Lifecycle rules:
- The sidebar shows the ``# title``, never the filename.
- Success: every item checked + ``<!-- auto:done=ok -->``; the ``4/4``
  lingers ~5 minutes, then it is swept. Visibility without accumulation.
- Cancelled: ``<!-- auto:done=cancelled -->`` — terminal, swept shortly.
- Failed: partial progress stays honest (no marker). While the owner is
  alive it keeps its eyesore status; once abandoned it is swept.
- Abandoned (no update/heartbeat for ``ABANDONED_AFTER_S``): swept
  automatically — a halted agent or deleted side-chat never leaves a
  stale list behind. All-checked but unmarked + stale: given the done
  marker (the agent finished but forgot to say so).
- Crash orphans are swept at bridge startup via the same rule.
- Parallel tasks each get their own file; advancing one never touches
  another. If the file was deleted (``/todo clear`` or the agent cleaned
  up), nobody resurrects it.

Ownership / heartbeat: the file's mtime is the lease. The bridge touches
it on every step; agents touch it (or send an activity report naming it)
at least every few minutes while working. Silence for 30 minutes means
the task is gone and the list goes with it.
"""
from __future__ import annotations

import os
import re
import time

from .paths import TODOS_DIR, ensure_dirs

AUTO_PREFIX = "_auto_"
AUTO_SUFFIX = ".md"
DONE_MARKER = "<!-- auto:done=%s -->"  # %s: ok | failed | cancelled
META_RE = re.compile(r"<!--\s*auto:rid=(\S+)\s+owner=(\S+)\s+t0=(\S+)\s*-->")
DONE_RE = re.compile(r"<!--\s*auto:done=(\w+)\s*-->")
TITLE_RE = re.compile(r"^#\s+(.+?)\s*$")

#: A todo file nobody touched for this long is abandoned and gets swept.
ABANDONED_AFTER_S = 30 * 60
#: Completed (done marker) lists linger this long for visibility, then sweep.
COMPLETED_LINGER_S = 5 * 60


def slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:28] or "task"


def _safe_id(rid: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", rid or "")[:12]
    return safe or "x"


def auto_name(rid: str, title: str = "") -> str:
    return f"{AUTO_PREFIX}{slugify(title)}-{_safe_id(rid)}{AUTO_SUFFIX}"


def is_auto(name: str) -> bool:
    return (name.startswith(AUTO_PREFIX) and name.endswith(AUTO_SUFFIX)
            and len(name) > len(AUTO_PREFIX) + len(AUTO_SUFFIX))


_ITEM_RE = re.compile(r"^(\s*[-*]\s+)\[( |x|X)\](.*)$")


def parse(path: str) -> dict | None:
    """Parse a todo file. None when missing/unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    rid, owner, t0, done = "", "", 0.0, ""
    m = META_RE.search(text)
    if m:
        rid, owner, t0s = m.group(1), m.group(2), m.group(3)
        try:
            t0 = float(t0s)
        except ValueError:
            t0 = 0.0
    dm = DONE_RE.search(text)
    if dm:
        done = dm.group(1)
    title = ""
    items: list[tuple[str, bool, str]] = []
    for ln in text.splitlines():
        if not title:
            tm = TITLE_RE.match(ln)
            if tm:
                title = tm.group(1)
                continue
        im = _ITEM_RE.match(ln)
        if im:
            items.append((im.group(1), im.group(2).lower() == "x",
                          im.group(3)))
    return {"rid": rid, "owner": owner, "t0": t0, "done_marker": done,
            "title": title, "items": items, "mtime": mtime, "text": text}


def _iter_auto() -> list[tuple[str, str]]:
    try:
        names = os.listdir(TODOS_DIR)
    except OSError:
        return []
    return [(n, os.path.join(TODOS_DIR, n)) for n in names if is_auto(n)]


def find_path(rid: str) -> str | None:
    """Locate a task's file by its rid marker (names carry only a slug)."""
    for _, path in _iter_auto():
        info = parse(path)
        if info and info["rid"] == rid:
            return path
    return None


def create(rid: str, title: str, steps: list[str], owner: str) -> str:
    """Create the auto todo list for a task; sweep older completed ones.

    Returns the file path — the bridge keeps it for advance/finish.
    """
    ensure_dirs()
    title = (title or rid).strip() or rid
    owner = (owner or "unknown").strip() or "unknown"
    items = steps if steps else [title]
    header = [f"<!-- auto:rid={rid} owner={owner} t0={time.time():.0f} -->",
              f"# {title}"]
    path = os.path.join(TODOS_DIR, auto_name(rid, title))
    lines = list(header)
    for s in items:
        lines.append(f"- [ ] {s}")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass
    sweep_completed()
    return path


def touch(path: str) -> None:
    """Heartbeat: mark the file as still owned."""
    try:
        os.utime(path, None)
    except OSError:
        pass


def advance(path: str, done: int) -> None:
    """Check the first ``done`` items. Never resurrects a deleted file,
    never unchecks anything (manual TUI toggles survive)."""
    info = parse(path)
    if info is None:
        return  # deleted by /todo clear or the agent — stay deleted
    seen = 0
    out = []
    changed = False
    for ln in info["text"].splitlines():
        m = _ITEM_RE.match(ln)
        if m and seen < done and m.group(2) == " ":
            seen += 1
            changed = True
            out.append(f"{m.group(1)}[x]{m.group(3)}")
        else:
            if m:
                seen += 1
            out.append(ln)
    if not changed:
        touch(path)  # still alive — refresh the lease
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError:
        pass


def toggle_item(path: str, index: int) -> bool | None:
    """Flip item ``index``; returns the new checked state (None: no file)."""
    info = parse(path)
    if info is None:
        return None
    out = []
    cur = 0
    new_state: bool | None = None
    for ln in info["text"].splitlines():
        m = _ITEM_RE.match(ln)
        if m and cur == index:
            new_state = m.group(2) == " "
            out.append(f"{m.group(1)}[{'x' if new_state else ' '}]{m.group(3)}")
        else:
            out.append(ln)
        if m:
            cur += 1
    if new_state is None:
        return None
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError:
        return None
    return new_state


def finish(path: str, ok: bool, note: str = "") -> None:
    """Record the terminal state.

    - ok: check everything, ``done=ok`` marker (lingers, then swept).
    - cancelled: ``done=cancelled`` marker — terminal, swept shortly.
    - failed: partial progress stays exactly as it was, no marker; the
      abandonment sweeper clears it once the owner goes quiet.
    """
    info = parse(path)
    if info is None:
        return
    lines = info["text"].splitlines()
    if ok:
        lines = [(_ITEM_RE.sub(lambda m: f"{m.group(1)}[x]{m.group(3)}", ln)
                  if _ITEM_RE.match(ln) else ln) for ln in lines]
        lines.append(DONE_MARKER % "ok")
    elif note == "cancelled":
        lines.append(f"> cancelled")
        lines.append(DONE_MARKER % "cancelled")
    else:
        if note:
            lines.append(f"> {note[:120]}")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError:
        pass


def mark_done(path: str, state: str = "ok") -> None:
    """Agent contract: call when the task is done (or just delete the file)."""
    info = parse(path)
    if info is None or info["done_marker"]:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(DONE_MARKER % state + "\n")
    except OSError:
        pass


def sweep_completed() -> int:
    """Delete done-marker lists whose visibility moment has passed."""
    now = time.time()
    n = 0
    for _, path in _iter_auto():
        info = parse(path)
        if info is None or not info["done_marker"]:
            continue
        if now - info["mtime"] > COMPLETED_LINGER_S:
            try:
                os.remove(path)
                n += 1
            except OSError:
                pass
    return n


def sweep_abandoned(now: float | None = None,
                   live_rids: set | None = None) -> dict:
    """Drop lists nobody is holding any more.

    - stale + incomplete + no done marker → deleted (halted agent,
      deleted side-chat, crashed task).
    - stale + all checked + no marker → given the done marker (the agent
      finished but forgot to say so; it then lingers briefly).
    - live_rids: request ids currently running in this process — never
      swept, even if their file went quiet mid-command.
    Returns {"deleted": [...], "completed": [...]} (filenames).
    """
    now = now if now is not None else time.time()
    live = live_rids or set()
    deleted, completed = [], []
    for name, path in _iter_auto():
        info = parse(path)
        if info is None or info["done_marker"]:
            continue
        if info["rid"] in live:
            continue
        if now - info["mtime"] < ABANDONED_AFTER_S:
            continue
        items = info["items"]
        if items and all(c for _, c, _ in items):
            mark_done(path)
            completed.append(name)
        else:
            try:
                os.remove(path)
                deleted.append(name)
            except OSError:
                pass
    return {"deleted": deleted, "completed": completed}


def sweep_orphans() -> dict:
    """Bridge startup: same abandonment rule — a restart never shows a
    stale list, and a live task (fresh mtime) is never touched."""
    return sweep_abandoned()
