import { isAuthError } from '~/composables/useApi'
import { useBilling, isAccountLocked, accountLockedMessage } from '~/composables/useBilling'

export const FIX_PAYMENT_STARTED_KEY = 'fix-payment-started-at'
// Newest failed payment_recovery.attempted_at (epoch ms) the user has already been shown, so a
// stale decline is not shown again as a fresh one after the round trip to Stripe.
export const FIX_PAYMENT_SEEN_FAILED_KEY = 'fix-payment-seen-failed-at'

// Shared by /billing and the layout banner. The marker is both the fallback portal-return
// signal and the staleness guard for payment_recovery.attempted_at.
export function usePortal() {
  const { openBillingPortal } = useBilling()
  const { refreshSubscription } = useSubscription()
  const portalRedirecting = ref(false)
  const portalError = ref<string | null>(null)
  // True when the last attempt failed with account_locked; portalError then holds the server's
  // detail. Pages switch to their locked state on it.
  const portalLocked = ref(false)

  async function manageBilling(): Promise<void> {
    if (portalRedirecting.value) return
    portalError.value = null
    portalLocked.value = false
    portalRedirecting.value = true
    try {
      const { url } = await openBillingPortal()
      if (!isValidPortalUrl(url)) throw new Error('Unexpected billing portal URL')
      try {
        sessionStorage.setItem(FIX_PAYMENT_STARTED_KEY, Date.now().toString())
      } catch {
        // Storage unavailable: the ?from=portal return URL still works.
      }
      window.location.href = url
    } catch (err: unknown) {
      portalRedirecting.value = false
      if (isAuthError(err)) return
      const e = err as { status?: number }
      if (isAccountLocked(err)) {
        portalLocked.value = true
        portalError.value = accountLockedMessage(err)
        await refreshSubscription()
      } else if (e?.status === 409) {
        portalError.value = "Billing management isn't available for this account."
        await refreshSubscription()
      } else if (e?.status === 429) {
        portalError.value = 'Too many attempts. Please wait a moment and try again.'
      } else {
        portalError.value = "We couldn't open the billing page just now. Please try again in a moment."
      }
    }
  }

  return { portalRedirecting, portalError, portalLocked, manageBilling }
}
