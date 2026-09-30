"""Real pairing between muse-cli and the Muse app.

The old `setup` flow printed a pairing code and hoped the assistant knew
what to do with it. This module implements an actual handshake:

1. The CLI creates a pairing request: a human-readable code plus a random
   nonce, written to ``~/.muse/pairing/request.json``.
2. The user pastes the copyable prompt (:func:`build_prompt`) into the
   Muse app.
3. The assistant proves it can reach THIS machine's bridge by writing
   ``~/.muse/pairing/receipt.json`` echoing the nonce back.
4. :func:`check_receipt` validates code + nonce, records the pairing in
   ``~/.muse/paired.json`` and cleans up the handshake files.

The nonce echo proves liveness: a stale or copied receipt can't pair.
``muse-cli unpair`` wipes pairing state (and the CLI identity) so the
next launch re-enters the flow — the equivalent of logging out.
"""
from __future__ import annotations

import json
import os
import secrets
import time

from . import __version__
from . import paths

_PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I/L/O/0/1


def pairing_code() -> str:
    raw = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def ensure_identity() -> dict:
    """Load the CLI identity, creating it if needed (stable machine id)."""
    try:
        with open(paths.IDENTITY_PATH, encoding="utf-8") as f:
            ident = json.load(f)
        if isinstance(ident, dict) and ident.get("cli_id"):
            return ident
    except (OSError, ValueError):
        pass
    ident = {
        "cli_id": secrets.token_hex(16),
        "created_at": int(time.time()),
        "version": __version__,
    }
    os.makedirs(paths.MUSE_HOME, exist_ok=True)
    with open(paths.IDENTITY_PATH, "w", encoding="utf-8") as f:
        json.dump(ident, f, indent=2)
    return ident


def is_paired() -> bool:
    try:
        with open(paths.PAIRED_PATH, encoding="utf-8") as f:
            rec = json.load(f)
        return isinstance(rec, dict) and bool(rec.get("paired"))
    except (OSError, ValueError):
        return False


def paired_info() -> dict | None:
    try:
        with open(paths.PAIRED_PATH, encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) and rec.get("paired") else None
    except (OSError, ValueError):
        return None


def load_request() -> dict | None:
    try:
        with open(paths.PAIRING_REQUEST_PATH, encoding="utf-8") as f:
            req = json.load(f)
        return req if isinstance(req, dict) and req.get("nonce") else None
    except (OSError, ValueError):
        return None


def new_request(cli_id: str) -> dict:
    """Create (or replace) the pairing request. Returns the request dict."""
    req = {
        "code": pairing_code(),
        "nonce": secrets.token_hex(16),
        "cli_id": cli_id,
        "created_at": int(time.time()),
        "version": __version__,
    }
    os.makedirs(paths.PAIRING_DIR, exist_ok=True)
    with open(paths.PAIRING_REQUEST_PATH, "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2)
    return req


def ensure_request(cli_id: str) -> dict:
    """Return the live pairing request, creating one if there isn't any."""
    return load_request() or new_request(cli_id)


def check_receipt() -> dict | None:
    """Validate ``pairing/receipt.json`` against the live request.

    On success the pairing is recorded in ``paired.json``, the handshake
    files are removed, and the receipt dict is returned. Anything else
    (no receipt, wrong code, wrong/missing nonce) returns None and leaves
    the request in place so the user can retry.
    """
    req = load_request()
    if req is None:
        return None
    try:
        with open(paths.PAIRING_RECEIPT_PATH, encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("code") != req.get("code"):
        return None
    nonce = rec.get("nonce")
    if not nonce or nonce != req.get("nonce"):
        return None
    paired = {
        "paired": True,
        "code": req.get("code"),
        "cli_id": req.get("cli_id"),
        "muse": rec.get("muse") or "muse-app",
        "paired_at": int(time.time()),
    }
    with open(paths.PAIRED_PATH, "w", encoding="utf-8") as f:
        json.dump(paired, f, indent=2)
    for p in (paths.PAIRING_REQUEST_PATH, paths.PAIRING_RECEIPT_PATH):
        try:
            os.remove(p)
        except OSError:
            pass
    return rec


def unpair() -> None:
    """Wipe pairing state and the CLI identity: next launch re-pairs."""
    for p in (paths.PAIRED_PATH, paths.PAIRING_REQUEST_PATH,
              paths.PAIRING_RECEIPT_PATH, paths.IDENTITY_PATH):
        try:
            os.remove(p)
        except OSError:
            pass


def build_prompt(req: dict) -> str:
    """The copyable prompt the user pastes into the Muse app.

    It carries the code + machine identity and instructs the assistant
    to (1) prove bridge access via the nonce handshake and (2) install
    the message bridge (watcher) so the link stays alive.
    """
    code = req.get("code", "?")
    cli_id = req.get("cli_id", "?")
    bridge = paths.MUSE_HOME
    return f"""Connect this machine's muse-cli to the Muse app (pairing).

Pairing code: {code}
CLI id: {cli_id}
Bridge directory: {bridge}  (on this machine's filesystem)
Protocol: https://github.com/TheBikramLama/muse-cli/blob/main/PROTOCOL.md

To complete pairing, prove you can reach this machine's bridge files,
then keep the link alive:

1. Read {bridge}/pairing/request.json — it holds
   {{"code", "nonce", "cli_id", ...}}. Confirm the code matches {code}
   above; abort if it doesn't.

2. Write {bridge}/pairing/receipt.json:
   {{"code": "{code}", "nonce": "<echo the nonce back exactly>",
     "muse": "<your assistant name>", "at": <unix epoch seconds>}}
   Echoing the nonce proves this handshake is live — never reuse a receipt.

3. Set up the message bridge so this machine stays connected. About every
   2 minutes:
   - Write the heartbeat {bridge}/watcher.json =
     {{"at": <now>, "ok": true, "state": "idle", "mid": null, "error": null}}
     (use "state": "writing" and the message id while composing a reply).
   - List {bridge}/messages/*.json — each is
     {{"id", "from": "tui", "text", "at", "session"}}. For every new one,
     write {bridge}/replies/<id>.json =
     {{"id": "<same id>", "from": "muse", "text": "<your reply>",
       "at": <now>, "session": "<same session tag>"}}. Markdown is fine;
     it renders as a card in the TUI.
   - Keep your own seen-list so no message is answered twice.

4. Say so in chat once the first heartbeat is written — the CLI is
   watching for your receipt and will show "connected".

5. Todos (maximum visibility): the CLI renders ~/.muse/todos/*.md live in
   its sidebar — update them as you go, don't just report at the end.
   When your bridge request runs, the bridge auto-creates
   ~/.muse/todos/_auto_<slug>-<id>.md from its steps and checks items
   off; for finer-grained progress rewrite that same file yourself,
   flipping "- [ ]" to "- [x]". For conversational work with no bridge
   request, keep your own _auto_ file (marker line first, then a real
   "# Title", then real step labels — never "step 1"). Touch it every few
   minutes while live; append <!-- auto:done=ok --> (or delete it) when
   finished. Never leave a stale 2/4.

6. Be visible while you work: the bridge only sees terminal commands,
   so heartbeat your non-terminal work with
   `muse-cli report --agent <id> --task "..." --status "..."`
   (re-run as it changes; it also heartbeats your todo file). The TUI
   shows you live in its activity line and agents panel. Full protocol:
   PROTOCOL.md.

Keep replies short. Never invent bridge results; report what the files
actually say."""
