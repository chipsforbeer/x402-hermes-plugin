# venice-x402 — pay for Venice AI from a USDC wallet, inside Hermes Agent

A [Hermes Agent](https://hermes-agent.nousresearch.com) plugin that lets Hermes use
[Venice AI](https://venice.ai) **without an API key**. Every request is signed by a
wallet, and Venice charges that wallet's prepaid credit. The credit is topped up with
USDC on Base via [x402](https://docs.venice.ai/guides/integrations/x402-venice-api),
from a status-bar chip in the Hermes desktop app, which also prompts you when the
credit runs low.

## How it works

- **Every message:** the provider signs a fresh Sign-In-With-X (EIP-4361) message for
  each HTTP request instead of sending an API key. Venice checks the signature and
  debits that wallet's credit. No on-chain transaction per message.
- **Top-ups:** the wallet signs an EIP-3009 `transferWithAuthorization` for USDC on
  Base. Venice's relayer submits it and pays the gas, so **the wallet needs no ETH**.
  The credit is usable immediately.
- The credit belongs to the wallet address and is separate from any venice.ai
  account balance.

## What's in the repo

| Path | Installs to | Does |
|---|---|---|
| `plugin/venice-x402/` | `~/.hermes/plugins/venice-x402` | settings (wallet key, alert level, caps), `/venice` + `/venice-topup`, balance events, desktop backend (`dashboard/plugin_api.py`) and desktop UI (`desktop/plugin.js`: status-bar chip + low-credit prompt) |
| `provider/venice-x402/` | `~/.hermes/plugins/model-providers/venice-x402` | model provider **Venice (x402 wallet)**: signs every Venice request with the wallet |
| `venice_x402_test.py` | — | standalone protocol test outside Hermes (`wallet`, `balance` and `inspect` are free; `chat` uses a little Venice credit; `topup` pays real USDC and asks you to type "yes" first) |

Shared logic lives in `plugin/venice-x402/venice_x402_core.py`.

## Install (development)

```sh
git clone https://github.com/chipsforbeer/x402-hermes-plugin && cd x402-hermes-plugin
ln -sn "$PWD/plugin/venice-x402"   ~/.hermes/plugins/venice-x402
ln -sn "$PWD/provider/venice-x402" ~/.hermes/plugins/model-providers/venice-x402
hermes plugins enable venice-x402   # installs eth-account into Hermes' environment
hermes gateway restart              # needed so the running gateway loads it
```

Then in the desktop app:

1. **Capabilities → Plugins → Rescan**, and switch on **Venice x402**.
2. **Settings → Plugins → Venice x402**: paste the wallet's private key (stored in
   Hermes' secret store, not in `config.yaml`), and adjust the alert level and caps.
3. Fund the wallet with **USDC on Base** and top up from the status-bar chip
   (Venice's minimum is $5).
4. In the model picker, choose provider **Venice (x402 wallet)**.

Tested with Hermes Agent v0.23.3 (desktop, macOS).

## Safety

- Pays only exact-scheme USDC on Base, and only to Venice's pinned payment address;
  refuses if Venice's 402 quote names a different payee.
- Per-top-up cap (default $20, hard maximum $100) and a rolling 24h cap (default $50).
- One top-up at a time; every attempt is logged to
  `~/.hermes/plugin-data/venice-x402/topups.jsonl`.
- Nothing pays without a click (desktop) or a confirmation (`/venice-topup`).
- The private key is used only to sign; it is never sent anywhere.

**Use a dedicated wallet** holding only what you're willing to spend. This is
experimental, unaudited software. It is not affiliated with Venice AI or Nous Research.

## Tests

```sh
uv run --no-project --with pytest --with eth-account --with httpx pytest -q tests/
```

See [ROADMAP.md](ROADMAP.md) for status and the OpenRouter plan.
