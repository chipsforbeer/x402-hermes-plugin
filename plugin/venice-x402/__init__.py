"""Venice x402 — keep a wallet-funded Venice credit balance topped up from Hermes.

Agent half: watches Venice calls made through the ``venice-x402`` provider, pushes
balance updates to the desktop half, and adds ``/venice`` and ``/venice-topup``.
"""
from __future__ import annotations

import logging
import threading
import time

from . import venice_x402_core as _core_mod

logger = logging.getLogger("venice_x402")
PROVIDER = "venice-x402"


def _core():
    import sys

    return sys.modules.setdefault("venice_x402_core", _core_mod)


_refresh_state = {"last": 0.0, "running": False}


def _broadcast(event: str, payload: dict) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(_core().PLUGIN_ID, event, payload)
    except Exception:
        logger.debug("venice-x402: broadcast failed", exc_info=True)


def refresh_and_broadcast(force: bool = False) -> None:
    """Re-read the balance in the background and tell the desktop half."""
    now = time.time()
    if _refresh_state["running"] or (not force and now - _refresh_state["last"] < 20):
        return
    _refresh_state.update(running=True, last=now)

    def run():
        try:
            core = _core()
            core.invalidate_balance()
            st = core.status()
            _broadcast("balance", st)
        finally:
            _refresh_state["running"] = False

    threading.Thread(target=run, name="venice-x402-balance", daemon=True).start()


def _is_ours(kwargs) -> bool:
    return str(kwargs.get("provider") or "").strip().lower() == PROVIDER


def _on_post_api_request(**kwargs):
    if _is_ours(kwargs):
        refresh_and_broadcast()


def _on_api_request_error(**kwargs):
    if _is_ours(kwargs) and kwargs.get("status_code") == 402:
        _broadcast("empty", {"message": "Venice credit ran out."})
        refresh_and_broadcast(force=True)


def _fmt_status(st: dict) -> str:
    if not st.get("configured"):
        return "Venice x402: no wallet configured (Settings → Plugins → Venice x402)."
    if "balance_usd" not in st:
        return f"Venice x402 wallet {st['address']}: {st.get('error', 'balance unavailable')}"
    s = st["settings"]
    return (f"Venice credit: ${st['balance_usd']:.4f} (wallet {st['address'][:6]}…{st['address'][-4:]})\n"
            f"Low-balance alert below ${s['low_balance_usd']:g} · cap ${s['max_topup_usd']:g}/top-up, "
            f"${s['daily_limit_usd']:g}/24h · topped up in last 24h: ${st['spent_24h_usd']:g}")


def _expose_wallet_address() -> None:
    """Hermes' model picker lists an api-key provider only when one of its env vars is set.
    Publish the wallet's PUBLIC address under the provider's env var so "Venice (x402 wallet)"
    shows up. Never the private key."""
    import os

    try:
        acct = _core().account()
    except Exception:
        return
    if acct is not None:
        os.environ["VENICE_X402_WALLET_ADDRESS"] = acct.address


def register(ctx):
    _expose_wallet_address()
    ctx.register_hook("post_api_request", _on_post_api_request)
    ctx.register_hook("api_request_error", _on_api_request_error)

    def venice_cmd(raw_args: str) -> str:
        st = _core().status()
        return _fmt_status(st)

    def topup_cmd(raw_args: str) -> str:
        core = _core()
        s = core.settings()
        words = raw_args.split()
        confirmed = "confirm" in words
        words = [w for w in words if w != "confirm"]
        try:
            amount = float(words[0].lstrip("$")) if words else float(s["default_topup_usd"])
        except ValueError:
            return "Usage: /venice-topup [amount in USD] [confirm]"
        try:
            core.check_topup_allowed(amount)
            acct = core.require_account()
        except core.VeniceX402Error as exc:
            return str(exc)
        answer = "yes" if confirmed else ctx.ask_choice(
            f"Pay ${amount:g} USDC (Base) from {acct.address[:6]}…{acct.address[-4:]} to Venice for credit?",
            [{"value": "yes", "label": f"Top up ${amount:g}"}, {"value": "no", "label": "Cancel"}],
        )
        if answer is None:
            return f"Couldn't ask for confirmation here. Run `/venice-topup {amount:g} confirm` to pay."
        if answer != "yes":
            return "Top-up cancelled. Nothing was signed."
        try:
            res = core.topup(amount)
        except core.VeniceX402Error as exc:
            return str(exc)
        refresh_and_broadcast(force=True)
        _broadcast("topup", res)
        return (f"Topped up ${res['amount_usd']:g}. Venice credit now ${res['new_balance_usd']}."
                + (f"\n{res['tx_url']}" if res.get("tx_url") else ""))

    ctx.register_command("venice", venice_cmd, description="Show Venice x402 wallet credit")
    ctx.register_command("venice-topup", topup_cmd, description="Top up Venice credit with USDC",
                         args_hint="[usd]")
