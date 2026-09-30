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
        "PAIRING_DIR": str(home / ".muse" / "pairing"),
        "PAIRING_REQUEST_PATH": str(home / ".muse" / "pairing" / "request.json"),
        "PAIRING_RECEIPT_PATH": str(home / ".muse" / "pairing" / "receipt.json"),
        "PAIRED_PATH": str(home / ".muse" / "paired.json"),
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
