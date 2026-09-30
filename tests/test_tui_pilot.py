"""Textual pilot tests for the muse-cli TUI.

Run with:  /tmp/testvenv/bin/python -m pytest tests/test_tui_pilot.py -x -q
Screenshots land in tests/shots/ as SVG (convert to PNG with cairosvg).
"""
import asyncio
import os

import pytest

from muse_cli import paths

SHOTS = os.path.join(os.path.dirname(__file__), "shots")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    # Minimal set: the bridge/app/protocol/sessions modules bind these
    # directly. Extra dirs are created so nothing touches the real ~/.muse.
    dirs = ["queue", "results", "sessions", "skills", "scripts", "cancel",
            "approval", "exports", "messages", "replies", "todos",
            "tui-cmd", "status"]
    mapping = {
        "MUSE_HOME": str(home / ".muse"),
        "STATUS_DIR": str(home / ".muse" / "status"),
        "QUEUE_DIR": str(home / ".muse" / "queue"),
        "RESULTS_DIR": str(home / ".muse" / "results"),
        "SESSIONS_DIR": str(home / ".muse" / "sessions"),
        "CANCEL_DIR": str(home / ".muse" / "cancel"),
        "APPROVAL_DIR": str(home / ".muse" / "approval"),
        "TODOS_DIR": str(home / ".muse" / "todos"),
        "MESSAGES_DIR": str(home / ".muse" / "messages"),
        "REPLIES_DIR": str(home / ".muse" / "replies"),
        "PAIRING_DIR": str(home / ".muse" / "pairing"),
        "PAIRING_REQUEST_PATH": str(home / ".muse" / "pairing" / "request.json"),
        "PAIRING_RECEIPT_PATH": str(home / ".muse" / "pairing" / "receipt.json"),
        "PAIRED_PATH": str(home / ".muse" / "paired.json"),
        "INPUT_HISTORY_PATH": str(home / ".muse" / "input_history"),
        "ALL_DIRS": [str(home / ".muse" / d) for d in dirs],
    }
    # Every muse_cli submodule binds path names directly at import;
    # patch them all so nothing touches the real ~/.muse.
    import pkgutil
    import muse_cli
    for _mod in pkgutil.iter_modules(muse_cli.__path__):
        mod = __import__(f"muse_cli.{_mod.name}", fromlist=["*"])
        for attr, val in mapping.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val, raising=False)
    for d in mapping["ALL_DIRS"]:
        os.makedirs(d, exist_ok=True)
    # Main-UI tests run as a paired CLI; the pairing flow has its own test.
    import json
    from muse_cli import pairing as _pairing
    _ident = _pairing.ensure_identity()
    _req = _pairing.ensure_request(_ident["cli_id"])
    with open(mapping["PAIRING_RECEIPT_PATH"], "w") as f:
        json.dump({"code": _req["code"], "nonce": _req["nonce"],
                   "muse": "pilot", "at": 1}, f)
    assert _pairing.check_receipt() is not None
    return home


def _shot(pilot, name):
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name + ".svg")
    svg = pilot.app.export_screenshot()
    with open(path, "w") as f:
        f.write(svg)
    assert os.path.exists(path)
    return path


@pytest.mark.asyncio
async def test_app_boots_empty(fake_home):
    from muse_cli.app import MuseCliApp
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        _shot(pilot, "01-boot-empty")
        # header + modeline + input should exist
        assert app.query_one("#topline")
        assert app.query_one("#modeline")
        assert app.query_one("#cmd")


@pytest.mark.asyncio
async def test_task_card_lifecycle(fake_home):
    from muse_cli.app import MuseCliApp
    from muse_cli.protocol import submit
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        rid = submit("Pilot task", cmd=["echo", "hello"], cwd="/tmp",
                     source="local", session="main")
        await pilot.pause(2.0)  # let the bridge pick it up and finish
        _shot(pilot, "02-task-card-done")
        cards = list(app.query("TaskCard"))
        assert len(cards) == 1, f"expected 1 card, got {len(cards)}"
        card = cards[0]
        assert card.done, "card should be done after echo finishes"


@pytest.mark.asyncio
async def test_running_card_and_realtime_status(fake_home):
    from muse_cli.app import MuseCliApp, TaskCard
    from muse_cli.protocol import submit
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        rid = submit("Slow task",
                     cmd=["python3", "-c", "import time; time.sleep(5)"],
                     cwd=str(fake_home), source="local", session="main")
        # wait for the bridge to pick it up (poll until a running card exists)
        cards = []
        for _ in range(40):
            await pilot.pause(0.25)
            cards = [c for c in app.query("TaskCard") if not c.done]
            if cards:
                break
        assert cards, "expected a running card"
        card = cards[0]
        # agent pushes realtime status
        sp = os.path.join(paths.STATUS_DIR, rid + ".txt")
        with open(sp, "w") as f:
            f.write("warming up\nprocessing 50%\n")
        await pilot.pause(1.0)  # ticks pick it up
        assert card._line == "processing 50%", f"got {card._line!r}"
        _shot(pilot, "03-running-card-status")
        # c reveals the command while running
        assert card.toggle_detail() is True
        await pilot.pause(0.3)
        _shot(pilot, "04-running-card-detail")
        assert "time.sleep(5)" in card._detail.text


@pytest.mark.asyncio
async def test_command_palette(fake_home):
    from muse_cli.app import MuseCliApp
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        await pilot.press("ctrl+p")
        await pilot.pause(0.5)
        _shot(pilot, "05-palette")
        # palette should be visible and not steal Esc
        await pilot.press("escape")
        await pilot.pause(0.3)
        _shot(pilot, "06-palette-closed")


@pytest.mark.asyncio
async def test_sidebar_toggle(fake_home):
    from muse_cli.app import MuseCliApp
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        # create a todo file so the sidebar has content
        with open(os.path.join(paths.TODOS_DIR, "demo.md"), "w") as f:
            f.write("# Demo\n- [x] done thing\n- [ ] todo thing\n")
        await pilot.pause(1.5)
        _shot(pilot, "07-sidebar")


@pytest.mark.asyncio
async def test_late_worker_thread_calls_are_dropped_after_shutdown(fake_home):
    """Regression: a worker thread that outlives app shutdown (e.g. the 5s
    sleeper in test_running_card_and_realtime_status finishing after its
    test's app closed) must not schedule onto the dead loop — Textual
    orphans the callback coroutine, surfacing as 'coroutine ... was never
    awaited' (RuntimeWarning) in whatever test runs next."""
    from textual.app import App as _TextualApp
    from muse_cli.app import MuseCliApp
    app = MuseCliApp(session="main")
    calls = []
    orig = _TextualApp.call_from_thread

    def spy(self, callback, *args, **kwargs):
        calls.append(getattr(callback, "__name__", callback))
        return None

    _TextualApp.call_from_thread = spy
    try:
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.2)
            n0 = len(calls)
        # App is shut down now: late worker-thread calls are dropped.
        app._safe_call(app._refresh_statusbar)
        app.notify("late toast")
        assert len(calls) == n0, calls
    finally:
        _TextualApp.call_from_thread = orig


@pytest.mark.asyncio
async def test_clear_wipes_history_and_restart_stays_clear(fake_home):
    """Regression: /clear must wipe the persisted feed state (session
    JSONL + message/reply files), not just the on-screen cards — otherwise
    a restart brings everything back."""
    import json
    from muse_cli import pairing as _pairing
    from muse_cli.app import MuseCliApp
    from muse_cli.sessions import log

    # Pair (idempotent) in case an earlier test unpaired.
    _ident = _pairing.ensure_identity()
    _req = _pairing.ensure_request(_ident["cli_id"])
    with open(paths.PAIRING_RECEIPT_PATH, "w") as f:
        json.dump({"code": _req["code"], "nonce": _req["nonce"],
                   "muse": "pilot", "at": 1}, f)
    assert _pairing.check_receipt() is not None

    # Seed persisted history: one finished task, one message, one reply.
    log("20260930-120000", {"type": "task", "id": "t1", "task": "Old task",
                            "source": "local", "cmd": ["echo", "hi"],
                            "cwd": "/tmp", "ok": True, "exit": 0,
                            "duration_s": 0.1, "summary": "ok"})
    with open(os.path.join(paths.MESSAGES_DIR, "m1.json"), "w") as f:
        json.dump({"id": "m1", "from": "tui", "text": "hello muse",
                   "at": 1, "session": "main"}, f)
    with open(os.path.join(paths.REPLIES_DIR, "m1.json"), "w") as f:
        json.dump({"id": "m1", "from": "muse", "text": "hello back",
                   "at": 2, "session": "main"}, f)

    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        assert app.query("TaskCard"), "seeded task should restore on boot"
        assert app.query("MessageCard"), "seeded messages should restore"
        # Drive /clear through the real input box.
        cmd = app.query_one("#cmd")
        cmd.focus()
        cmd.value = "/clear"
        await pilot.press("enter")
        await pilot.pause(0.5)
        assert not list(app.query("TaskCard")), "task cards should be gone"
        assert not list(app.query("MessageCard")), "msg cards should be gone"
        assert os.listdir(paths.SESSIONS_DIR) == [], "session files wiped"
        assert os.listdir(paths.MESSAGES_DIR) == [], "messages wiped"
        assert os.listdir(paths.REPLIES_DIR) == [], "replies wiped"
    # A fresh launch must stay clear — the reported bug.
    app2 = MuseCliApp(session="main")
    async with app2.run_test(size=(120, 36)) as pilot2:
        await pilot2.pause(0.5)
        assert not list(app2.query("TaskCard")), "no task cards after restart"
        assert not list(app2.query("MessageCard")), "no msg cards after restart"


@pytest.mark.asyncio
async def test_todo_clear(fake_home):
    """/todo clear <name> deletes one list; bare /todo clear deletes all."""
    import json
    from muse_cli import pairing as _pairing
    from muse_cli.app import MuseCliApp

    _ident = _pairing.ensure_identity()
    _req = _pairing.ensure_request(_ident["cli_id"])
    with open(paths.PAIRING_RECEIPT_PATH, "w") as f:
        json.dump({"code": _req["code"], "nonce": _req["nonce"],
                   "muse": "pilot", "at": 1}, f)
    assert _pairing.check_receipt() is not None

    for name in ("alpha.md", "beta.md"):
        with open(os.path.join(paths.TODOS_DIR, name), "w") as f:
            f.write(f"# {name}\n- [x] done thing\n- [ ] todo thing\n")
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(1.5)  # the 1s todo poll picks the files up
        assert set(app._todo_data) == {"alpha.md", "beta.md"}
        done, total = app._todo_counts()
        assert (done, total) == (2, 4)

        async def run_slash(text):
            cmd = app.query_one("#cmd")
            cmd.focus()
            cmd.value = text
            await pilot.press("enter")
            await pilot.pause(0.5)

        await run_slash("/todo clear alpha")
        assert not os.path.exists(os.path.join(paths.TODOS_DIR, "alpha.md"))
        assert os.path.exists(os.path.join(paths.TODOS_DIR, "beta.md"))
        assert set(app._todo_data) == {"beta.md"}
        assert app._todo_counts() == (1, 2)

        await run_slash("/todo clear nosuch")
        assert os.path.exists(os.path.join(paths.TODOS_DIR, "beta.md"))

        await run_slash("/todo clear")
        assert os.listdir(paths.TODOS_DIR) == []
        assert app._todo_data == {}
        assert app._todo_counts() == (0, 0)


@pytest.mark.asyncio
async def test_pairing_screen_flow(fake_home):
    """Unpaired launch shows the pairing screen; a valid receipt dismisses it."""
    import json
    from muse_cli import pairing
    from muse_cli.app import MuseCliApp
    from muse_cli.pairing_screen import PairingScreen
    # Undo the fixture's pairing: back to a fresh unpaired state.
    pairing.unpair()
    assert not pairing.is_paired()
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        assert isinstance(app.screen, PairingScreen), type(app.screen)
        req = pairing.load_request()
        assert req is not None and req["code"]
        _shot(pilot, "08-pairing-screen")
        # The Muse app completes the handshake by writing the receipt.
        with open(paths.PAIRING_RECEIPT_PATH, "w") as f:
            json.dump({"code": req["code"], "nonce": req["nonce"],
                       "muse": "pilot", "at": 1}, f)
        await pilot.pause(4.0)  # 2s poll interval + 1.5s dismiss delay
        assert not isinstance(app.screen, PairingScreen), type(app.screen)
        assert pairing.is_paired()
        # Main UI is usable underneath.
        assert app.query_one("#topline")


@pytest.mark.asyncio
async def test_activity_reflects_external_work(fake_home):
    """The statusline is not Idle when other agents' work is in flight.

    Covers: live claimed queue files, agent-pushed status lines, queued
    (unclaimed) files, parked approvals, and the watcher's writing state.
    """
    import json
    import time
    from muse_cli.app import MuseCliApp
    from muse_cli.bridge import CLAIM_SUFFIX
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        assert app._external_activity_text() is None  # truly idle

        # A queued request waiting for a bridge.
        with open(os.path.join(paths.QUEUE_DIR, "q1.json"), "w") as f:
            json.dump({"id": "q1", "task": "queued job"}, f)
        t = app._external_activity_text()
        assert t is not None and "1 queued" in t.plain, t.plain

        # Claimed by a live pid (this test process) = actively working.
        os.rename(os.path.join(paths.QUEUE_DIR, "q1.json"),
                  os.path.join(paths.QUEUE_DIR,
                               f"q1.json{CLAIM_SUFFIX}.{os.getpid()}"))
        t = app._external_activity_text()
        assert t is not None and "Working" in t.plain, t.plain

        # An agent-pushed status line surfaces as the live description.
        with open(os.path.join(paths.STATUS_DIR, "ext1.txt"), "w") as f:
            f.write("migrating the database\n")
        t = app._external_activity_text()
        assert t is not None and "migrating the database" in t.plain, t.plain

        # Parked approvals outrank working.
        with open(os.path.join(paths.APPROVAL_DIR, "a1.json"), "w") as f:
            json.dump({"id": "a1"}, f)
        t = app._external_activity_text()
        assert t is not None and "awaiting approval" in t.plain, t.plain
        os.remove(os.path.join(paths.APPROVAL_DIR, "a1.json"))

        # Stale claims (dead pid) are ignored, not shown as working.
        os.rename(os.path.join(paths.QUEUE_DIR,
                               f"q1.json{CLAIM_SUFFIX}.{os.getpid()}"),
                  os.path.join(paths.QUEUE_DIR,
                               f"q1.json{CLAIM_SUFFIX}.999999999"))
        os.remove(os.path.join(paths.STATUS_DIR, "ext1.txt"))
        with open(os.path.join(paths.QUEUE_DIR, "q2.json"), "w") as f:
            json.dump({"id": "q2"}, f)
        t = app._external_activity_text()
        assert t is not None and "1 queued" in t.plain, t.plain
        os.remove(os.path.join(paths.QUEUE_DIR,
                               f"q1.json{CLAIM_SUFFIX}.999999999"))
        os.remove(os.path.join(paths.QUEUE_DIR, "q2.json"))

        # The watcher composing a reply shows instead of Idle.
        app._watcher = {"state": "writing", "at": time.time(), "ok": True}
        t = app._external_activity_text()
        assert t is not None and "writing" in t.plain, t.plain
        # ... but a stale heartbeat does not.
        app._watcher = {"state": "writing", "at": time.time() - 3600,
                        "ok": True}
        assert app._external_activity_text() is None


@pytest.mark.asyncio
async def test_activity_shows_auto_todo_progress(fake_home):
    """An agent's auto todo checklist progress appears in the Working line."""
    import json
    from muse_cli.app import MuseCliApp
    from muse_cli.bridge import CLAIM_SUFFIX
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        # Another session's task, claimed by a live pid.
        with open(os.path.join(paths.QUEUE_DIR, "w1.json"), "w") as f:
            json.dump({"id": "w1", "task": "widget work"}, f)
        os.rename(os.path.join(paths.QUEUE_DIR, "w1.json"),
                  os.path.join(paths.QUEUE_DIR,
                               f"w1.json{CLAIM_SUFFIX}.{os.getpid()}"))
        # Its system-managed todo list, 1 of 2 done.
        with open(os.path.join(paths.TODOS_DIR, "_auto_w1.md"), "w") as f:
            f.write("# widget work\n- [x] scaffold\n- [ ] tests\n")
        app._poll_todos()
        t = app._external_activity_text()
        assert t is not None and "Working" in t.plain, t.plain
        assert "1/2" in t.plain, t.plain
        # A fully-checked list no longer contributes progress.
        with open(os.path.join(paths.TODOS_DIR, "_auto_w1.md"), "w") as f:
            f.write("# widget work\n- [x] scaffold\n- [x] tests\n")
        app._poll_todos()
        t = app._external_activity_text()
        assert t is not None and "1/2" not in t.plain, t.plain


@pytest.mark.asyncio
async def test_update_nag_banner(fake_home):
    from muse_cli.app import MuseCliApp
    app = MuseCliApp(session="main")
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.5)
        banner = app.query_one("#updatebanner")
        assert app._update_status is None
        app._on_update_status({"behind": True, "local": "a" * 40,
                               "remote": "b" * 40, "branch": "main"})
        await pilot.pause()
        assert app._update_status is not None
        assert app._update_status["behind"] is True
        assert "press u" in str(banner.render())
        # Back up to date: nag clears.
        app._on_update_status({"behind": False, "local": "a" * 40,
                               "remote": "a" * 40, "branch": "main"})
        await pilot.pause()
        assert app._update_status is None
