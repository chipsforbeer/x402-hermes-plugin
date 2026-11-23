// Venice x402 — desktop half. Status-bar credit chip + low-credit prompt with a Top up button.
// Talks to dashboard/plugin_api.py (ctx.rest) and listens for plugin.venice-x402.* events.
import { host, STATUSBAR_AREAS } from '@hermes/plugin-sdk'

const ID = 'venice-x402'
const SNOOZE_MS = 30 * 60 * 1000 // "Not now" hides the prompt for 30 min (or until a top-up)
const POLL_MS = 60 * 1000
const PAY_TOAST = 'venice-x402-pay'

const money = v => (typeof v === 'number' ? `$${v < 10 ? v.toFixed(2) : v.toFixed(0)}` : '—')
const short = a => (a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '')

export default {
  id: ID,
  name: 'Venice x402',
  register(ctx) {
    let status = null
    let disposeChip = null
    let promptOpen = false
    let busy = false

    const snoozedUntil = () => Number(ctx.storage.get('snoozedUntil', 0)) || 0

    // ── top-up ──────────────────────────────────────────────────────────
    async function doTopup(amount) {
      if (busy) return
      busy = true
      renderChip()
      // one toast id for the whole flow: each notify() with it replaces the previous one
      host.notify({ id: PAY_TOAST, kind: 'info', title: 'Venice x402', message: `Paying $${amount} USDC…`, durationMs: 0 })
      try {
        const res = await ctx.rest('/topup', { method: 'POST', body: { amount_usd: amount }, timeoutMs: 120000 })
        if (!res || !res.ok) {
          host.notify({ id: PAY_TOAST, kind: 'error', title: 'Top-up failed', message: (res && res.error) || 'Unknown error' })
          return
        }
        ctx.storage.remove('snoozedUntil')
        if (res.status) update(res.status, { quiet: true })
        host.notify({
          id: PAY_TOAST,
          kind: 'success',
          title: 'Venice credit topped up',
          message: `+$${res.amount_usd} → ${money(Number(res.new_balance_usd))}`,
          action: res.tx_url ? { label: 'View transaction', onClick: () => ctx.os.openExternal(res.tx_url) } : undefined
        })
      } catch (err) {
        host.notify({ id: PAY_TOAST, kind: 'error', title: 'Top-up request failed',
          message: 'Check the balance before retrying.', detail: String((err && err.message) || err) })
      } finally {
        busy = false
        renderChip()
      }
    }

    // ── low-credit prompt ───────────────────────────────────────────────
    function prompt(reason) {
      if (promptOpen || busy || !status || !status.configured) return
      if (Date.now() < snoozedUntil()) return
      promptOpen = true
      const amount = status.settings ? status.settings.default_topup_usd : 10
      const msg = reason || `Credit is ${money(status.balance_usd)} (alert below ${money(status.settings.low_balance_usd)}).`
      host.notify({
        kind: 'warning',
        title: 'Venice credit low',
        message: msg,
        detail: `Pays $${amount} USDC on Base from ${short(status.address)}.`,
        action: { label: `Top up $${amount}`, onClick: () => { promptOpen = false; doTopup(amount) } },
        secondaryAction: {
          label: 'Not now',
          onClick: () => { promptOpen = false; ctx.storage.set('snoozedUntil', Date.now() + SNOOZE_MS) }
        },
        onDismiss: () => { promptOpen = false }
      })
      // Native notification too, for when Hermes is in the background.
      try {
        ctx.os.notify({ title: 'Venice credit low', body: msg })
      } catch (_) {}
    }

    // ── status-bar chip ─────────────────────────────────────────────────
    function renderChip() {
      if (disposeChip) disposeChip()
      const s = status
      let label = 'Venice …'
      let detail = 'Loading Venice x402 balance'
      if (s && !s.configured) {
        label = 'Venice: set wallet'
        detail = 'Add the wallet key in Settings → Plugins → Venice x402'
      } else if (s && typeof s.balance_usd === 'number') {
        label = `${s.low ? '⚠ ' : ''}Venice ${money(s.balance_usd)}`
        detail = `Wallet ${short(s.address)} · $${s.spent_24h_usd || 0} topped up in last 24h`
      } else if (s && s.error) {
        label = 'Venice ?'
        detail = s.error
      }
      if (busy) label = 'Venice: paying…'
      const amounts = [5, 10, 20].filter(a => !s || !s.settings || a <= s.settings.max_topup_usd)
      disposeChip = ctx.register({
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 125,
        data: {
          id: `${ID}:chip`,
          label,
          title: detail,
          toggleLabel: 'Venice credit',
          variant: 'menu',
          menuAlign: 'end',
          menuItems: [
            ...amounts.map(a => ({
              id: `topup-${a}`,
              label: `Top up $${a}`,
              disabled: busy || !s || !s.configured,
              onSelect: () => doTopup(a)
            })),
            { id: 'refresh', label: 'Refresh balance', onSelect: () => load(true) },
            {
              id: 'settings',
              label: 'Settings…',
              onSelect: () => host.navigate(`/settings?tab=plugins&plugin=${ID}`)
            }
          ]
        }
      })
    }

    function update(next, { quiet = false } = {}) {
      const prev = status
      status = next
      renderChip()
      if (!quiet && next && next.low && (!prev || !prev.low || prev.balance_usd !== next.balance_usd)) prompt()
    }

    async function load(refresh = false) {
      try {
        update(await ctx.rest(`/status${refresh ? '?refresh=true' : ''}`))
      } catch (err) {
        update({ configured: true, error: 'Plugin backend unavailable (is venice-x402 enabled?)' }, { quiet: true })
      }
    }

    // ── wiring ──────────────────────────────────────────────────────────
    ctx.onEvent(`plugin.${ID}.balance`, ev => update(ev && ev.payload))
    ctx.onEvent(`plugin.${ID}.empty`, () => prompt('Venice credit ran out — requests will fail until you top up.'))
    ctx.onEvent(`plugin.${ID}.settings.changed`, () => load(true))
    ctx.onEvent(`plugin.${ID}.topup`, () => load(true))
    ctx.setInterval(() => load(false), POLL_MS)
    ctx.onDispose(() => disposeChip && disposeChip())

    renderChip()
    load(true)
  }
}
