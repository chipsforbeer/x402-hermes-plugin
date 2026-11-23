"""Venice x402 core: wallet sign-in, credit balance, USDC top-ups.

Shared by the general plugin (hooks, slash commands, desktop backend) and the
model-provider plugin (per-request wallet sign-in). One module instance per
process: everything loads it through ``load_core()`` keyed in ``sys.modules``.

Money rules (enforced here, not in the UI):
  * pay only exact-scheme USDC on Base, only to the pinned Venice address
  * per-top-up cap and rolling 24h cap from the plugin settings
  * one top-up at a time; every attempt is logged to plugin-data
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger("venice_x402")

PLUGIN_ID = "venice-x402"
SECRET_ENV = "VENICE_X402_PRIVATE_KEY"
API = "https://api.venice.ai/api/v1"
BASE_RPC = "https://mainnet.base.org"
BASE_CHAIN_ID = 8453
BASE_CAIP2 = "eip155:8453"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Venice's x402 receiver, as returned by api.venice.ai's 402 challenge (2026-11-23).
VENICE_PAY_TO = "0x2670b922ef37c7df47158725c0cc407b5382293f"
MIN_TOPUP_USD = 5.0
ABSOLUTE_MAX_TOPUP_USD = 100.0  # no setting can raise a single top-up above this

DEFAULTS: dict[str, Any] = {
    "low_balance_usd": 1.0,
    "default_topup_usd": 10.0,
    "max_topup_usd": 20.0,
    "daily_limit_usd": 50.0,
}

_topup_lock = threading.Lock()
_balance_lock = threading.Lock()
_balance_cache: dict[str, Any] = {"at": 0.0, "value": None}
_acct_cache: dict[str, Any] = {"key_fp": None, "acct": None}


class VeniceX402Error(Exception):
    """A user-facing failure (message is safe to show)."""


# ── settings / secret ────────────────────────────────────────────────────────
def settings() -> dict[str, Any]:
    out = dict(DEFAULTS)
    try:
        from hermes_cli.config import load_config_readonly

        entry = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
        for k, v in (entry.get("settings") or {}).items():
            if k in DEFAULTS and v not in (None, ""):
                out[k] = float(v)
    except Exception:  # settings are optional; defaults are safe
        logger.debug("venice-x402: settings read failed", exc_info=True)
    out["max_topup_usd"] = min(max(out["max_topup_usd"], MIN_TOPUP_USD), ABSOLUTE_MAX_TOPUP_USD)
    return out


def _private_key() -> str | None:
    try:
        from agent.secret_scope import get_secret

        key = (get_secret(SECRET_ENV) or "").strip()
    except Exception:
        import os

        key = (os.environ.get(SECRET_ENV) or "").strip()
    if key and not key.startswith("0x"):
        key = "0x" + key
    return key or None


def account():
    """The wallet, or None when no key is configured. Never logs the key."""
    key = _private_key()
    if not key:
        return None
    import hashlib

    fp = hashlib.sha256(key.encode()).hexdigest()
    if _acct_cache["key_fp"] != fp:
        from eth_account import Account

        _acct_cache.update(key_fp=fp, acct=Account.from_key(key))
    return _acct_cache["acct"]


def require_account():
    acct = account()
    if acct is None:
        raise VeniceX402Error(
            "No wallet configured. Add the private key in Settings → Plugins → Venice x402.")
    return acct


# ── encoding helpers ─────────────────────────────────────────────────────────
def b64json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _hex(sig) -> str:
    s = sig.hex()
    return s if s.startswith("0x") else "0x" + s


# ── wallet sign-in (SIGN-IN-WITH-X), fresh per request ───────────────────────
def siwx_header(request_url: str, acct=None) -> str:
    from eth_account.messages import encode_defunct

    acct = acct or require_account()
    now = datetime.now(timezone.utc)
    f = {
        "domain": "api.venice.ai",
        "address": acct.address,
        "statement": "Sign in to Venice AI",
        "uri": request_url,
        "version": "1",
        "chainId": BASE_CAIP2,
        "type": "eip191",
        "nonce": secrets.token_hex(16),
        "issuedAt": _iso(now),
        "expirationTime": _iso(now + timedelta(minutes=5)),
    }
    message = (
        f"{f['domain']} wants you to sign in with your Ethereum account:\n{f['address']}\n\n"
        f"{f['statement']}\n\nURI: {f['uri']}\nVersion: {f['version']}\nChain ID: {BASE_CHAIN_ID}\n"
        f"Nonce: {f['nonce']}\nIssued At: {f['issuedAt']}\nExpiration Time: {f['expirationTime']}"
    )
    sig = _hex(acct.sign_message(encode_defunct(text=message)).signature)
    return b64json({**f, "signature": sig, "message": message})


# ── balances ─────────────────────────────────────────────────────────────────
def get_balance(*, max_age: float = 15.0) -> dict[str, Any]:
    """Venice credit for the wallet: {balanceUsd, canConsume, minimumTopUpUsd, ...}."""
    with _balance_lock:
        cached = _balance_cache["value"]
        if cached is not None and time.time() - _balance_cache["at"] < max_age:
            return cached
    acct = require_account()
    url = f"{API}/x402/balance/{acct.address}"
    r = httpx.get(url, headers={"SIGN-IN-WITH-X": siwx_header(url, acct)}, timeout=15)
    if r.status_code != 200:
        raise VeniceX402Error(f"Venice balance check failed (HTTP {r.status_code}).")
    data = r.json()["data"]
    with _balance_lock:
        _balance_cache.update(at=time.time(), value=data)
    return data


def invalidate_balance() -> None:
    with _balance_lock:
        _balance_cache.update(at=0.0, value=None)


def wallet_usdc(address: str) -> float:
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    r = httpx.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                   "params": [{"to": USDC_BASE, "data": data}, "latest"]}, timeout=15)
    r.raise_for_status()
    return int(r.json()["result"], 16) / 1e6


def status() -> dict[str, Any]:
    """Everything the UI shows. Never raises."""
    s = settings()
    out: dict[str, Any] = {"configured": True, "settings": s, "spent_24h_usd": spent_last_24h()}
    try:
        acct = account()
    except ImportError:
        out["error"] = "Signing library not loaded yet. Run `hermes gateway restart` (or fully quit Hermes)."
        return out
    except Exception:
        out["error"] = "Wallet key is invalid. Re-enter it in Settings → Plugins → Venice x402."
        return out
    out["configured"] = acct is not None
    if acct is None:
        return out
    out["address"] = acct.address
    # Public address only: lets Hermes' model picker list the venice-x402 provider.
    import os
    os.environ["VENICE_X402_WALLET_ADDRESS"] = acct.address
    try:
        b = get_balance()
        out["balance_usd"] = float(b["balanceUsd"])
        out["low"] = out["balance_usd"] < s["low_balance_usd"]
    except Exception as exc:  # network etc.
        out["error"] = str(exc) if isinstance(exc, VeniceX402Error) else "Venice unreachable"
    return out


# ── spend log ────────────────────────────────────────────────────────────────
def _log_path():
    try:
        from plugins.plugin_storage import plugin_data_dir

        return plugin_data_dir(PLUGIN_ID) / "topups.jsonl"
    except Exception:
        from pathlib import Path

        p = Path.home() / ".hermes" / "plugin-data" / PLUGIN_ID
        p.mkdir(parents=True, exist_ok=True)
        return p / "topups.jsonl"


def _log(entry: dict[str, Any]) -> None:
    entry = {"ts": time.time(), **entry}
    with open(_log_path(), "a") as fh:
        fh.write(json.dumps(entry) + "\n")


def spent_last_24h() -> float:
    cutoff = time.time() - 86400
    total = 0.0
    try:
        with open(_log_path()) as fh:
            for line in fh:
                e = json.loads(line)
                # count anything that might have moved money: success or unknown outcome
                if e.get("ts", 0) >= cutoff and e.get("status") in ("ok", "unknown"):
                    total += float(e.get("amount_usd", 0))
    except FileNotFoundError:
        pass
    return round(total, 6)


# ── top-up ───────────────────────────────────────────────────────────────────
def _sign_authorization(acct, pay_to: str, value: int, timeout_s: int, extra: dict) -> tuple[str, dict]:
    from eth_account import Account

    now = int(time.time())
    auth = {"from": acct.address, "to": pay_to, "value": value, "validAfter": now - 600,
            "validBefore": now + int(timeout_s or 300), "nonce": "0x" + secrets.token_hex(32)}
    signed = Account.sign_typed_data(acct.key, full_message={
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": extra.get("name", "USD Coin"), "version": extra.get("version", "2"),
                   "chainId": BASE_CHAIN_ID, "verifyingContract": USDC_BASE},
        "message": auth,
    })
    return _hex(signed.signature), {k: str(v) for k, v in auth.items()}


def check_topup_allowed(amount_usd: float) -> None:
    s = settings()
    if not (amount_usd == amount_usd) or amount_usd < MIN_TOPUP_USD:  # NaN-safe
        raise VeniceX402Error(f"Venice's minimum top-up is ${MIN_TOPUP_USD:g}.")
    if amount_usd > s["max_topup_usd"]:
        raise VeniceX402Error(f"${amount_usd:g} is above your per-top-up cap of ${s['max_topup_usd']:g}.")
    spent = spent_last_24h()
    if spent + amount_usd > s["daily_limit_usd"] + 1e-9:
        raise VeniceX402Error(
            f"Daily limit reached: ${spent:g} topped up in the last 24h, limit ${s['daily_limit_usd']:g}.")


def topup(amount_usd: float) -> dict[str, Any]:
    """Pay `amount_usd` USDC (Base) to Venice for wallet credit. Raises VeniceX402Error."""
    amount_usd = round(float(amount_usd), 2)
    if not _topup_lock.acquire(blocking=False):
        raise VeniceX402Error("A top-up is already in progress.")
    try:
        acct = require_account()
        check_topup_allowed(amount_usd)

        r = httpx.post(f"{API}/x402/top-up", json={}, timeout=20)
        if r.status_code != 402:
            raise VeniceX402Error(f"Unexpected reply from Venice (HTTP {r.status_code}).")
        ch = r.json() if "accepts" in (r.text or "") else json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))
        req = next((a for a in ch.get("accepts", [])
                    if a.get("scheme") == "exact" and a.get("network") == BASE_CAIP2
                    and str(a.get("asset", "")).lower() == USDC_BASE.lower()), None)
        if req is None:
            raise VeniceX402Error("Venice no longer offers USDC-on-Base top-ups.")
        if str(req.get("payTo", "")).lower() != VENICE_PAY_TO:
            _log({"status": "refused", "amount_usd": amount_usd, "reason": "payee changed", "payTo": req.get("payTo")})
            raise VeniceX402Error(
                f"Refusing to pay: Venice's payment address changed to {req.get('payTo')}. "
                "Check Venice's docs and update the plugin before topping up.")
        if amount_usd < int(req["amount"]) / 1e6:
            raise VeniceX402Error(f"Venice's minimum top-up is ${int(req['amount']) / 1e6:g}.")
        usdc = wallet_usdc(acct.address)
        if usdc + 1e-9 < amount_usd:
            raise VeniceX402Error(f"Wallet has ${usdc:.2f} USDC on Base; need ${amount_usd:g}.")

        value = round(amount_usd * 1e6)
        sig, auth = _sign_authorization(acct, req["payTo"], value, req.get("maxTimeoutSeconds") or 300,
                                        req.get("extra") or {})
        payload = {"x402Version": ch.get("x402Version", 2), "accepted": {**req, "amount": str(value)},
                   "payload": {"signature": sig, "authorization": auth}}
        if "resource" in ch:
            payload["resource"] = ch["resource"]
        try:
            r = httpx.post(f"{API}/x402/top-up", json={}, timeout=90,
                           headers={"PAYMENT-SIGNATURE": b64json(payload)})
        except httpx.HTTPError:
            # The signed authorization may still settle; count it against the daily limit.
            _log({"status": "unknown", "amount_usd": amount_usd, "reason": "network error after signing"})
            raise VeniceX402Error("Network error while paying. Check the balance before retrying.")
        invalidate_balance()
        if r.status_code != 200:
            try:
                body = r.json()
                reason = body.get("details") or body.get("message") or body.get("error")
            except Exception:
                reason = r.text[:200]
            _log({"status": "failed", "amount_usd": amount_usd, "http": r.status_code, "reason": reason})
            raise VeniceX402Error(f"Top-up failed: {reason}")
        tx = None
        if r.headers.get("PAYMENT-RESPONSE"):
            try:
                tx = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"])).get("transaction")
            except Exception:
                pass
        data = r.json().get("data", {})
        _log({"status": "ok", "amount_usd": amount_usd, "tx": tx, "payment_id": data.get("paymentId"),
              "new_balance": data.get("newBalance")})
        return {"amount_usd": amount_usd, "new_balance_usd": data.get("newBalance"), "tx": tx,
                "tx_url": f"https://basescan.org/tx/{tx}" if tx else None}
    finally:
        _topup_lock.release()
