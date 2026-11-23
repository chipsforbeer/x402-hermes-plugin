"""Offline tests for venice_x402_core (no network, no money).
Run: uv run --with pytest --with eth-account --with httpx pytest -q tests/"""
import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def core(tmp_path, monkeypatch):
    acct = Account.create()
    monkeypatch.setenv("VENICE_X402_PRIVATE_KEY", acct.key.hex())
    spec = importlib.util.spec_from_file_location("venice_x402_core_test", ROOT / "plugin/venice-x402/venice_x402_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_log_path", lambda: tmp_path / "topups.jsonl")
    mod._test_acct = acct
    return mod


def test_siwx_header_signed_by_wallet(core):
    h = json.loads(base64.b64decode(core.siwx_header("https://api.venice.ai/api/v1/chat/completions")))
    assert h["address"] == core._test_acct.address
    assert h["uri"] == "https://api.venice.ai/api/v1/chat/completions"
    assert f"Nonce: {h['nonce']}" in h["message"] and "Chain ID: 8453" in h["message"]
    signer = Account.recover_message(encode_defunct(text=h["message"]), signature=h["signature"])
    assert signer == core._test_acct.address


def test_nonces_are_fresh(core):
    a = json.loads(base64.b64decode(core.siwx_header("https://x")))
    b = json.loads(base64.b64decode(core.siwx_header("https://x")))
    assert a["nonce"] != b["nonce"]


def test_usdc_authorization_recovers_to_wallet(core):
    sig, auth = core._sign_authorization(core._test_acct, core.VENICE_PAY_TO, 5_000_000, 300,
                                         {"name": "USD Coin", "version": "2"})
    msg = {k: (int(v) if k in ("value", "validAfter", "validBefore") else v) for k, v in auth.items()}
    typed = {
        "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                   {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                  "TransferWithAuthorization": [{"name": n, "type": t} for n, t in
                      [("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"),
                       ("validBefore", "uint256"), ("nonce", "bytes32")]]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": core.USDC_BASE},
        "message": msg,
    }
    assert Account.recover_message(encode_typed_data(full_message=typed), signature=sig) == core._test_acct.address
    assert auth["to"].lower() == core.VENICE_PAY_TO and auth["value"] == "5000000"


def test_caps(core, monkeypatch):
    monkeypatch.setattr(core, "settings", lambda: {**core.DEFAULTS, "max_topup_usd": 20.0, "daily_limit_usd": 25.0})
    with pytest.raises(core.VeniceX402Error, match="minimum"):
        core.check_topup_allowed(4.99)
    with pytest.raises(core.VeniceX402Error, match="cap"):
        core.check_topup_allowed(21)
    with pytest.raises(core.VeniceX402Error, match="minimum"):
        core.check_topup_allowed(float("nan"))
    core.check_topup_allowed(20)
    core._log({"status": "ok", "amount_usd": 10})
    core._log({"status": "unknown", "amount_usd": 10})
    core._log({"status": "failed", "amount_usd": 10})
    assert core.spent_last_24h() == 20
    with pytest.raises(core.VeniceX402Error, match="Daily limit"):
        core.check_topup_allowed(10)
    core.check_topup_allowed(5)


def test_refuses_changed_payee(core, monkeypatch):
    class R:
        status_code = 402
        headers = {}
        text = '"accepts"'
        def json(self):
            return {"x402Version": 2, "accepts": [{"scheme": "exact", "network": "eip155:8453", "amount": "5000000",
                    "asset": core.USDC_BASE, "payTo": "0x000000000000000000000000000000000000dEaD",
                    "maxTimeoutSeconds": 300, "extra": {}}]}
    calls = []
    monkeypatch.setattr(core.httpx, "post", lambda *a, **k: calls.append(k) or R())
    with pytest.raises(core.VeniceX402Error, match="payment address changed"):
        core.topup(5)
    assert len(calls) == 1  # only the quote request; nothing signed or sent


def test_no_key_means_no_account(core, monkeypatch):
    monkeypatch.delenv("VENICE_X402_PRIVATE_KEY")
    assert core.account() is None
    with pytest.raises(core.VeniceX402Error, match="No wallet"):
        core.require_account()
