"""Desktop backend for venice-x402 — mounted at /api/plugins/venice-x402/."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


def _core():
    mod = sys.modules.get("venice_x402_core")
    if mod is None:
        path = Path(__file__).resolve().parent.parent / "venice_x402_core.py"
        spec = importlib.util.spec_from_file_location("venice_x402_core", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["venice_x402_core"] = mod
        spec.loader.exec_module(mod)
    return mod


def _broadcast(event: str, payload: dict) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event("venice-x402", event, payload)
    except Exception:
        pass


@router.get("/status")
def status(refresh: bool = False):
    core = _core()
    if refresh:
        core.invalidate_balance()
    return core.status()


class TopUp(BaseModel):
    amount_usd: float


@router.post("/topup")
def topup(body: TopUp):
    core = _core()
    try:
        res = core.topup(body.amount_usd)
    except core.VeniceX402Error as exc:
        # 200 + ok:false so the desktop half can show the exact reason
        return {"ok": False, "error": str(exc)}
    st = core.status()
    _broadcast("balance", st)
    return {"ok": True, **res, "status": st}
