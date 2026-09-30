"""Tests for muse_cli.pairing: the real Muse-app handshake."""
import json
import os
import re

import pytest

from muse_cli import pairing
from muse_cli import paths


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home" / ".muse"
    mapping = {
        "MUSE_HOME": str(home),
        "PAIRING_DIR": str(home / "pairing"),
        "PAIRING_REQUEST_PATH": str(home / "pairing" / "request.json"),
        "PAIRING_RECEIPT_PATH": str(home / "pairing" / "receipt.json"),
        "PAIRED_PATH": str(home / "paired.json"),
        "IDENTITY_PATH": str(home / "cli-identity.json"),
    }
    for attr, val in mapping.items():
        monkeypatch.setattr(paths, attr, val, raising=False)
    return home


def _write_receipt(code, nonce, muse="test-muse"):
    with open(paths.PAIRING_RECEIPT_PATH, "w") as f:
        json.dump({"code": code, "nonce": nonce, "muse": muse,
                   "at": 1790752400}, f)


def test_pairing_code_format():
    code = pairing.pairing_code()
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", code), code


def test_ensure_identity_creates_and_keeps(fake_home):
    a = pairing.ensure_identity()
    assert re.fullmatch(r"[0-9a-f]{32}", a["cli_id"])
    b = pairing.ensure_identity()
    assert b["cli_id"] == a["cli_id"]


def test_not_paired_initially(fake_home):
    assert not pairing.is_paired()
    assert pairing.paired_info() is None


def test_request_lifecycle(fake_home):
    assert pairing.load_request() is None
    req = pairing.new_request("cli123")
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", req["code"])
    assert len(req["nonce"]) == 32
    assert pairing.load_request()["nonce"] == req["nonce"]
    # ensure_request reuses the live one
    assert pairing.ensure_request("cli123")["nonce"] == req["nonce"]


def test_check_receipt_without_receipt(fake_home):
    pairing.new_request("cli123")
    assert pairing.check_receipt() is None
    assert not pairing.is_paired()


def test_check_receipt_rejects_wrong_code(fake_home):
    req = pairing.new_request("cli123")
    _write_receipt("ZZZZ-9999", req["nonce"])
    assert pairing.check_receipt() is None
    assert not pairing.is_paired()
    # request survives so the user can retry
    assert pairing.load_request() is not None


def test_check_receipt_rejects_wrong_nonce(fake_home):
    req = pairing.new_request("cli123")
    _write_receipt(req["code"], "0" * 32)
    assert pairing.check_receipt() is None
    assert not pairing.is_paired()


def test_check_receipt_rejects_missing_nonce(fake_home):
    req = pairing.new_request("cli123")
    with open(paths.PAIRING_RECEIPT_PATH, "w") as f:
        json.dump({"code": req["code"], "muse": "x"}, f)
    assert pairing.check_receipt() is None
    assert not pairing.is_paired()


def test_successful_pairing(fake_home):
    req = pairing.new_request("cli123")
    _write_receipt(req["code"], req["nonce"], muse="muse-test")
    rec = pairing.check_receipt()
    assert rec is not None and rec["muse"] == "muse-test"
    assert pairing.is_paired()
    info = pairing.paired_info()
    assert info["cli_id"] == "cli123" and info["code"] == req["code"]
    # handshake files are cleaned up
    assert not os.path.exists(paths.PAIRING_REQUEST_PATH)
    assert not os.path.exists(paths.PAIRING_RECEIPT_PATH)
    assert os.path.exists(paths.PAIRED_PATH)


def test_unpair_resets_everything(fake_home):
    ident = pairing.ensure_identity()
    req = pairing.new_request(ident["cli_id"])
    _write_receipt(req["code"], req["nonce"])
    assert pairing.check_receipt() is not None
    assert pairing.is_paired()
    pairing.unpair()
    assert not pairing.is_paired()
    assert not os.path.exists(paths.PAIRED_PATH)
    assert not os.path.exists(paths.IDENTITY_PATH)


def test_build_prompt_contains_handshake(fake_home):
    req = pairing.new_request("cli123")
    prompt = pairing.build_prompt(req)
    assert req["code"] in prompt
    assert "cli123" in prompt
    assert "pairing/receipt.json" in prompt
    assert "nonce" in prompt
    assert "watcher.json" in prompt
    assert "PROTOCOL.md" in prompt
