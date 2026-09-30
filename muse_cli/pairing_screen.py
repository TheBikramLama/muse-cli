"""First-run pairing screen: connect the CLI to the Muse app.

Shown on top of the main UI when ``~/.muse/paired.json`` is missing. It
displays the pairing code and the copyable prompt for the Muse app, then
polls for the assistant's receipt. When the handshake validates, the
screen dismisses itself and the TUI continues normally.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Static, TextArea

from . import pairing


class PairingScreen(Screen):
    """Modal-ish first screen: paste the prompt into the Muse app."""

    BINDINGS = [
        ("y", "copy_prompt", "Copy prompt"),
    ]

    DEFAULT_CSS = """
    PairingScreen {
        align: center middle;
    }
    #pair-box {
        width: 76;
        height: auto;
        max-height: 90%;
        border: round $primary;
        padding: 1 2;
    }
    #pair-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }
    #pair-code {
        text-align: center;
        text-style: bold;
        color: $accent;
        margin: 1 0;
    }
    #pair-prompt {
        height: 12;
        border: solid $primary-muted;
        margin: 1 0;
    }
    #pair-status {
        text-align: center;
        margin-top: 1;
    }
    #pair-hint {
        text-align: center;
        color: $text-muted;
    }
    PairingScreen .pair-hint {
        text-align: center;
        color: $text-muted;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._prompt_text = ""
        self._poller = None

    def compose(self) -> ComposeResult:
        with Vertical(id="pair-box"):
            yield Static("Connect to Muse", id="pair-title")
            yield Static(
                "muse-cli isn't paired with the Muse app yet.\n"
                "Paste the prompt below into the Muse app —\n"
                "it will prove it can reach this machine and set up the link.",
                id="pair-hint")
            self.code = Static("", id="pair-code")
            yield self.code
            self.prompt_area = TextArea("", read_only=True, id="pair-prompt")
            yield self.prompt_area
            self.status = Static("", id="pair-status")
            yield self.status
            yield Static("y copy prompt · q quit", classes="pair-hint")

    def on_mount(self) -> None:
        ident = pairing.ensure_identity()
        req = pairing.ensure_request(ident["cli_id"])
        self._prompt_text = pairing.build_prompt(req)
        self.code.update(f"Pairing code:  {req['code']}")
        self.prompt_area.load_text(self._prompt_text)
        self.status.update("Waiting for the Muse app to complete pairing…")
        self._poller = self.set_interval(2.0, self._check)

    def _check(self) -> None:
        rec = pairing.check_receipt()
        if rec is None:
            return
        if self._poller is not None:
            self._poller.stop()
            self._poller = None
        muse = rec.get("muse") or "Muse app"
        self.status.update(f"Paired with {muse} ✓")
        try:
            self.app.notify(f"Paired with {muse} ✓")
        except Exception:
            pass
        # dismiss() returns an awaitable; don't let set_timer await it.
        def _later() -> None:
            self.dismiss()
        self.set_timer(1.5, _later)

    def action_copy_prompt(self) -> None:
        try:
            self.app.copy_to_clipboard(self._prompt_text)
            self.status.update("Prompt copied — paste it into the Muse app ✓")
        except Exception:
            self.status.update(
                "Copy failed — drag-select the prompt text with your mouse")
