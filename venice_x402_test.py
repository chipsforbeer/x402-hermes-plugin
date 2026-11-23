# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27", "eth-account>=0.13"]
# ///
"""
Venice x402 smoke test — proves the whole loop before we build the Hermes plugin.

    1. wallet    : show the wallet's USDC + ETH on Base (read-only, free)
    2. balance   : sign in with the wallet, read the Venice credit balance (free)
    3. inspect   : ask Venice for a top-up quote (the 402 challenge) — no money moves
    4. topup N   : sign a USDC payment for $N and send it  ← ONLY step that spends
    5. chat      : one chat call that must use a tool, paid from the credit balance

Run:   uv run venice_x402_test.py all            (steps 1,2,3,5 — never spends)
       uv run venice_x402_test.py topup 5        (asks you to type "yes" first)

The private key is read from VENICE_WALLET_PRIVATE_KEY or BASE_EOA_PRIVATE_KEY (env var or .env file).
It is never printed or sent anywhere — only signatures leave this machine.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

# ── constants ────────────────────────────────────────────────────────────────
API = "https://api.venice.ai/api/v1"
BASE_RPC = "https://mainnet.base.org"
BASE_CHAIN_ID = 8453
BASE_CAIP2 = "eip155:8453"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # native USDC on Base
HARD_CAP_USD = 20.0  # refuse any single top-up above this, whatever you type


# ── wallet ───────────────────────────────────────────────────────────────────
KEY_NAMES = ("VENICE_WALLET_PRIVATE_KEY", "BASE_EOA_PRIVATE_KEY")


def load_account():
    key = next((os.environ[n] for n in KEY_NAMES if os.environ.get(n)), None)
    env_file = Path(__file__).with_name(".env")
    if not key and env_file.exists():
        for line in env_file.read_text().splitlines():
            name, _, val = line.strip().partition("=")
            if name in KEY_NAMES and val:
                key = val.strip().strip('"').strip("'")
                break
    if not key:
        sys.exit(f"Set one of {KEY_NAMES} (env var or .env next to this script).")
    return Account.from_key(key)


def b64json(obj) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def iso(dt: datetime) -> str:
    # same shape as JavaScript's toISOString(): 2026-11-23T12:00:00.000Z
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ── "sign in with x": proves we own the wallet, fresh for every request ─────
def siwx_header(acct, request_url: str) -> str:
    now = datetime.now(timezone.utc)
    fields = {
        "domain": "api.venice.ai",
        "address": acct.address,  # checksummed
        "statement": "Sign in to Venice AI",
        "uri": request_url,
        "version": "1",
        "chainId": BASE_CAIP2,
        "type": "eip191",
        "nonce": secrets.token_hex(16),  # 32 hex chars, single use
        "issuedAt": iso(now),
        "expirationTime": iso(now + timedelta(minutes=5)),
    }
    # EIP-4361 (SIWE) text; the message uses the bare chain id
    message = (
        f"{fields['domain']} wants you to sign in with your Ethereum account:\n"
        f"{fields['address']}\n\n"
        f"{fields['statement']}\n\n"
        f"URI: {fields['uri']}\n"
        f"Version: {fields['version']}\n"
        f"Chain ID: {BASE_CHAIN_ID}\n"
        f"Nonce: {fields['nonce']}\n"
        f"Issued At: {fields['issuedAt']}\n"
        f"Expiration Time: {fields['expirationTime']}"
    )
    sig = acct.sign_message(encode_defunct(text=message)).signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    # current format = fields + signature; `message` kept for older server paths
    return b64json({**fields, "signature": sig, "message": message})


def auth_headers(acct, url: str) -> dict:
    return {"SIGN-IN-WITH-X": siwx_header(acct, url)}


# ── step 1: on-chain wallet check ────────────────────────────────────────────
def rpc(method, params):
    r = httpx.post(BASE_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    return r.json()["result"]


def step_wallet(acct):
    data = "0x70a08231" + acct.address[2:].lower().rjust(64, "0")  # balanceOf(address)
    usdc = int(rpc("eth_call", [{"to": USDC_BASE, "data": data}, "latest"]), 16) / 1e6
    eth = int(rpc("eth_getBalance", [acct.address, "latest"]), 16) / 1e18
    print(f"[wallet]  {acct.address}")
    print(f"          USDC on Base: ${usdc:,.2f}   ETH on Base: {eth:.6f}")
    return usdc


# ── step 2: Venice credit balance ────────────────────────────────────────────
def step_balance(acct):
    url = f"{API}/x402/balance/{acct.address}"
    r = httpx.get(url, headers=auth_headers(acct, url), timeout=20)
    if r.status_code != 200:
        print(f"[balance] HTTP {r.status_code}: {r.text[:500]}")
        return None
    d = r.json()["data"]
    print(f"[balance] Venice credit: ${d['balanceUsd']}  canConsume={d['canConsume']}  "
          f"min top-up ${d['minimumTopUpUsd']}  suggested ${d['suggestedTopUpUsd']}")
    return d


# ── step 3: get the 402 challenge (a price quote; nothing is paid) ───────────
def get_challenge():
    url = f"{API}/x402/top-up"
    r = httpx.post(url, json={}, timeout=20)
    if r.status_code != 402:
        sys.exit(f"[inspect] expected HTTP 402, got {r.status_code}: {r.text[:500]}")
    try:
        challenge = r.json()
    except ValueError:
        challenge = None
    if not (isinstance(challenge, dict) and "accepts" in challenge):
        raw = r.headers.get("PAYMENT-REQUIRED")
        if not raw:
            sys.exit("[inspect] 402 had no payment requirements")
        challenge = json.loads(base64.b64decode(raw))
    return challenge


def pick_base_usdc(challenge):
    for req in challenge["accepts"]:
        if req.get("scheme") == "exact" and req.get("network") in (BASE_CAIP2, "base") \
                and req.get("asset", "").lower() == USDC_BASE.lower():
            return req
    sys.exit("[inspect] Venice did not offer exact-scheme USDC on Base")


def step_inspect():
    ch = get_challenge()
    req = pick_base_usdc(ch)
    print(f"[inspect] x402Version={ch.get('x402Version')}  options={len(ch['accepts'])}")
    print(f"          Base USDC option: min ${int(req['amount']) / 1e6}  payTo={req['payTo']}  "
          f"timeout={req.get('maxTimeoutSeconds')}s  extra={req.get('extra')}")
    return ch, req


# ── step 4: pay (the only step that moves money) ─────────────────────────────
def sign_usdc_authorization(acct, pay_to: str, value: int, timeout_s: int, extra: dict | None):
    """EIP-3009 transferWithAuthorization: an off-chain signed cheque.
    Whoever submits it on-chain pays the gas; it can only move `value` USDC to `pay_to`."""
    now = int(time.time())
    auth = {
        "from": acct.address,
        "to": pay_to,
        "value": value,
        "validAfter": now - 600,
        "validBefore": now + int(timeout_s or 300),
        "nonce": "0x" + secrets.token_hex(32),
    }
    extra = extra or {}
    signed = Account.sign_typed_data(
        acct.key,
        full_message={
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": extra.get("name", "USD Coin"),
                "version": extra.get("version", "2"),
                "chainId": BASE_CHAIN_ID,
                "verifyingContract": USDC_BASE,
            },
            "message": auth,
        },
    )
    sig = signed.signature.hex()
    if not sig.startswith("0x"):
        sig = "0x" + sig
    return sig, {k: str(v) for k, v in auth.items()}


def step_topup(acct, amount_usd: float):
    if amount_usd > HARD_CAP_USD:
        sys.exit(f"[topup] ${amount_usd} is above the ${HARD_CAP_USD} hard cap in this script.")
    ch, req = step_inspect()
    minimum = int(req["amount"]) / 1e6
    if amount_usd < minimum:
        sys.exit(f"[topup] Venice minimum is ${minimum}.")
    usdc = step_wallet(acct)
    if usdc < amount_usd:
        sys.exit(f"[topup] wallet has ${usdc:.2f} USDC, need ${amount_usd}.")

    print(f"\n  About to pay ${amount_usd} USDC (Base) to {req['payTo']} for Venice credit.")
    if input("  Type 'yes' to sign and send: ").strip().lower() != "yes":
        sys.exit("[topup] cancelled — nothing was signed.")

    value = round(amount_usd * 1e6)
    sig, auth = sign_usdc_authorization(acct, req["payTo"], value, req.get("maxTimeoutSeconds"), req.get("extra"))
    accepted = {**req, "amount": str(value)}
    if ch.get("x402Version", 2) >= 2:
        payload = {"x402Version": ch.get("x402Version", 2), "accepted": accepted,
                   "payload": {"signature": sig, "authorization": auth}}
        if "resource" in ch:
            payload["resource"] = ch["resource"]
    else:
        payload = {"x402Version": 1, "scheme": "exact", "network": "base",
                   "payload": {"signature": sig, "authorization": auth}}

    r = httpx.post(f"{API}/x402/top-up", json={}, timeout=90,
                   headers={"PAYMENT-SIGNATURE": b64json(payload)})
    print(f"[topup] HTTP {r.status_code}: {r.text[:800]}")
    receipt = r.headers.get("PAYMENT-RESPONSE")
    if receipt:
        print(f"[topup] receipt: {json.loads(base64.b64decode(receipt))}")
    if r.status_code == 200:
        step_balance(acct)


# ── step 5: chat with a tool call, paid from credit ──────────────────────────
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current time in an IANA timezone.",
        "parameters": {"type": "object",
                       "properties": {"timezone": {"type": "string", "description": "e.g. Asia/Tokyo"}},
                       "required": ["timezone"]},
    },
}]


def pick_model(acct, wanted: str | None):
    if wanted:
        return wanted
    url = f"{API}/models"
    r = httpx.get(url, headers=auth_headers(acct, url), timeout=20)
    r.raise_for_status()
    for m in r.json().get("data", []):
        caps = (m.get("model_spec") or {}).get("capabilities") or {}
        if caps.get("supportsFunctionCalling"):
            return m["id"]
    sys.exit("[chat] no model advertises function calling; pass --model <id>")


def chat(acct, body):
    url = f"{API}/chat/completions"
    r = httpx.post(url, json=body, headers=auth_headers(acct, url), timeout=120)
    left = r.headers.get("X-Balance-Remaining")
    if r.status_code != 200:
        sys.exit(f"[chat] HTTP {r.status_code}: {r.text[:800]}")
    return r.json(), left


def step_chat(acct, model: str | None):
    model = pick_model(acct, model)
    print(f"[chat]    model: {model}")
    messages = [{"role": "user", "content": "What time is it in Tokyo right now? Use the tool."}]
    out, left = chat(acct, {"model": model, "messages": messages, "tools": TOOLS})
    msg = out["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        print(f"[chat]    ✗ model answered without calling the tool: {msg.get('content')!r}")
        return
    call = calls[0]
    args = json.loads(call["function"]["arguments"] or "{}")
    print(f"[chat]    ✓ tool call: {call['function']['name']}({args})")
    from zoneinfo import ZoneInfo
    result = datetime.now(ZoneInfo(args.get("timezone", "UTC"))).strftime("%H:%M %Z")
    messages += [msg, {"role": "tool", "tool_call_id": call["id"], "content": result}]
    out, left = chat(acct, {"model": model, "messages": messages, "tools": TOOLS})
    print(f"[chat]    ✓ final answer: {out['choices'][0]['message'].get('content')!r}")
    print(f"[chat]    tokens: {out.get('usage')}   credit left: ${left}")


# ── cli ──────────────────────────────────────────────────────────────────────
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    model = None
    if "--model" in argv:
        i = argv.index("--model")
        model = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    acct = load_account()
    cmd = argv[0]
    if cmd == "wallet":
        step_wallet(acct)
    elif cmd == "balance":
        step_balance(acct)
    elif cmd == "inspect":
        step_inspect()
    elif cmd == "topup":
        step_topup(acct, float(argv[1]))
    elif cmd == "chat":
        step_chat(acct, model)
    elif cmd == "all":
        step_wallet(acct)
        bal = step_balance(acct)
        step_inspect()
        if bal and bal.get("canConsume"):
            step_chat(acct, model)
        else:
            print("[chat]    skipped — no Venice credit yet. Run: uv run venice_x402_test.py topup 5")
    else:
        sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
