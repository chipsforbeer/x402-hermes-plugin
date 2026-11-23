# Roadmap

Idea: Hermes uses the AI provider as normal; the plugin keeps a wallet-funded credit
balance topped up with USDC via x402, with spend caps and a click to confirm.

## Phase 0 — prove the Venice loop (`venice_x402_test.py`) ✅
- [x] Wallet sign-in (SIGN-IN-WITH-X) accepted by Venice
- [x] 402 top-up quote parsed (x402 v2; USDC on Base or Solana; $5 minimum)
- [x] Real $5 top-up from a wallet holding **0 ETH** — Venice's relayer submits the
      EIP-3009 authorization and pays the gas
- [x] Chat + tool call paid from the credit
- [x] x402 credit is wallet-bound: it does not appear in a venice.ai account balance,
      even when the same wallet is linked to that account

## Phase 1 — Hermes plugin, Venice only ✅ (working in Hermes desktop)
- [x] Model provider `venice-x402`: every request signed by the wallet, no API key
- [x] Status-bar chip with live credit; low-credit prompt with Top up / Not now
- [x] `/venice` and `/venice-topup [usd] [confirm]`
- [x] Caps per top-up and per 24h, pinned payee, one top-up at a time, spend log
- [ ] Top up button exercised end-to-end from the desktop UI
- [ ] Optional auto top-up below the threshold (off by default)

## Phase 2 — OpenRouter (separate plugin)
- Inference stays API-key based; credits are bought via `POST /credits/purchases`
- Payment is MPP (not x402), USDC.e on Tempo; needs an organisation management key
  with billing permissions
- 5% fee, $10 minimum; credits arrive asynchronously (poll until `credited`), so
  top up earlier
- Reuse the chip / prompt / caps design; no custom model transport needed

## Later
- Solana wallets
- One combined "AI credits" chip across providers
- Other x402 inference sellers (e.g. Dreams Router)

## Hermes integration notes
- New plugin dependencies load only after `hermes gateway restart` (reopening the app
  is not enough).
- The model picker lists an api-key provider only when one of its env vars is set, so
  the plugin exports the wallet's PUBLIC address as `VENICE_X402_WALLET_ADDRESS`.
- Hermes' generic env seeding turned that address into a credential-pool "key" (401),
  so the provider overrides `seed_pool_credentials` → `[]`, and `create_client` accepts
  the sentinel or the wallet address.
- Hermes sends ~14.5k prompt tokens per turn → about $0.02 per message on
  `zai-org-glm-5-1`.
