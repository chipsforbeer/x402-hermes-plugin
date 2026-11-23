"""venice-x402 model provider — Venice inference paid from a wallet's x402 credit.

No API key: every HTTP request to Venice is signed by the wallet (SIGN-IN-WITH-X,
fresh nonce per request), and Venice charges that wallet's prepaid USDC credit.
The wallet key lives in the companion ``venice-x402`` plugin's settings
(``VENICE_X402_PRIVATE_KEY``); this module loads that plugin's core.
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from pathlib import Path
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger("venice_x402")

BASE_URL = "https://api.venice.ai/api/v1"
# The runtime "api key" for this provider. Not a secret: if any code path ever sent it as a
# Bearer token, Venice would just reject it. create_client() only takes over for this value,
# so a user's own `custom` Venice endpoint with a real API key is never re-routed here.
SENTINEL_KEY = "x402-wallet"


def _core():
    mod = sys.modules.get("venice_x402_core")
    if mod is not None:
        return mod
    from hermes_constants import get_hermes_home

    path = get_hermes_home() / "plugins" / "venice-x402" / "venice_x402_core.py"
    if not path.is_file():
        raise RuntimeError("venice-x402 provider needs the 'venice-x402' plugin installed")
    spec = importlib.util.spec_from_file_location("venice_x402_core", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["venice_x402_core"] = mod
    spec.loader.exec_module(mod)
    return mod


def _build_http_client(timeout: Any):
    import httpx

    def sign(request: httpx.Request) -> None:
        request.headers.pop("Authorization", None)
        url = str(request.url.copy_with(query=None))
        request.headers["SIGN-IN-WITH-X"] = _core().siwx_header(url)

    return httpx.Client(event_hooks={"request": [sign]}, timeout=timeout,
                        limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=30))


class VeniceX402Profile(ProviderProfile):
    def resolve_api_key_secret(self):
        try:
            if _core().account() is not None:
                return SENTINEL_KEY, "venice-x402 wallet"
        except Exception:
            logger.debug("venice-x402: wallet unavailable", exc_info=True)
        return None

    def missing_credentials_hint(self):
        return "Add the wallet key in Settings → Plugins → Venice x402 (VENICE_X402_PRIVATE_KEY)."

    def seed_pool_credentials(self, *, suppressed_sources, explicitly_configured):
        # No pool rows: the generic env seeding would turn VENICE_X402_WALLET_ADDRESS (public,
        # only there so the picker lists us) into a "key" and send it as a Bearer token.
        return []

    def _is_our_key(self, api_key: Any) -> bool:
        if api_key == SENTINEL_KEY:
            return True
        try:
            acct = _core().account()
        except Exception:
            return False
        return acct is not None and str(api_key or "").lower() == acct.address.lower()

    def create_client(self, **client_kwargs: Any):
        if not self._is_our_key(client_kwargs.get("api_key")):
            return None  # not our wallet route — let Hermes build its standard client
        import openai

        accepted = set(inspect.signature(openai.OpenAI.__init__).parameters) - {"self", "http_client"}
        kwargs = {k: v for k, v in client_kwargs.items() if k in accepted}
        kwargs["base_url"] = client_kwargs.get("base_url") or BASE_URL
        timeout = client_kwargs.get("timeout") or 600.0
        return openai.OpenAI(**kwargs, http_client=_build_http_client(timeout))

    def fetch_models(self, *, api_key=None, base_url=None, timeout: float = 8.0):
        # Venice's catalog is public; never send the sentinel as a Bearer token.
        return super().fetch_models(api_key=None, base_url=base_url, timeout=timeout)


register_provider(VeniceX402Profile(
    name="venice-x402",
    aliases=("venice-wallet",),
    display_name="Venice (x402 wallet)",
    description="Venice AI paid from a USDC wallet's prepaid credit (x402) — no API key",
    signup_url="https://docs.venice.ai/guides/integrations/x402-venice-api",
    # Deliberately NOT the private key: if wallet resolution ever failed, Hermes' generic key
    # ladder would read this var and could send it as a Bearer token. An address is harmless.
    env_vars=("VENICE_X402_WALLET_ADDRESS",),
    base_url=BASE_URL,
    auth_type="api_key",
    supports_health_check=False,
    default_aux_model="zai-org-glm-5-1",
))
