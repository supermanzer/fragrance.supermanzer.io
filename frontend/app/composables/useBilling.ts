import { useApi, isAuthError } from '~/composables/useApi'

export interface Plan {
  slug: string
  name: string
  amount_cents: number
  currency: string
  billing_interval: string
}

export interface PlanChangePending {
  // Either can be null when the target price is not one of our plans; render date-only.
  slug: string | null
  name: string | null
  effective_at: string
}

export interface PlanChangeState {
  pending: PlanChangePending | null
  // Empty when a change is pending or the subscription is set to cancel.
  available: Plan[]
  // Stripe's period end (not access_expires_at, which includes a grace window).
  current_period_end: string
  cancel_at_period_end: boolean
}

export interface SessionStatus {
  status: 'none' | 'checkout_started' | 'active' | 'revoked'
  plan: { slug: string; name: string } | null
  access_expires_at: string | null
  // The server's access verdict. status stays 'active' for a lapsed subscriber, so only this
  // says whether the layout will let them run anything. Do not key on `granted`: it is false
  // on the short-circuit path.
  has_active_access: boolean
  granted: boolean
  // Non-null only while lapsed and not revoked.
  lapse_reason?: 'payment_failed' | 'cancelled' | null
}

export function isPaymentPending(err: unknown): boolean {
  const e = err as { status?: number; data?: { code?: string } } | null
  return e?.status === 409 && e.data?.code === 'payment_pending'
}

export function isAccountLocked(err: unknown): boolean {
  const e = err as { status?: number; data?: { code?: string } } | null
  return e?.status === 409 && e.data?.code === 'account_locked'
}

// The server's `detail` for an account_locked 409 (each endpoint words it for itself). The
// static sentence is used only if a locked response ever arrives without its detail; the
// server's own wording is always preferred.
export function accountLockedMessage(err: unknown, supportEmail: string): string {
  const detail = (err as { data?: { detail?: unknown } } | null)?.data?.detail
  return typeof detail === 'string' && detail.trim() ? detail : lockedRestartSentence(supportEmail)
}

export function useBilling() {
  const { api } = useApi()
  const supportEmail = useSupportEmail()
  const plans = ref<Plan[]>([])
  const plansLoading = ref(false)
  const plansError = ref<string | null>(null)

  async function fetchPlans(): Promise<void> {
    plansLoading.value = true
    plansError.value = null
    try {
      const data = await api<{ plans: Plan[] }>('/billing/checkout/')
      plans.value = data.plans
    } catch (err: unknown) {
      if (isAuthError(err)) return
      plansError.value = 'We couldn\'t load billing plans. Please try again.'
    } finally {
      plansLoading.value = false
    }
  }

  // startCheckout re-throws on every failure (400/409/429/502/503) so the page
  // can branch on err.status — 409 renders the already-active panel, 429 gets
  // the slow-down copy, everything else gets the generic checkout-failure alert.
  // `resumed` is true when the url is an already-open session. A 409 payment_pending means a
  // prior payment is still being confirmed; the page handles it by polling the subscription.
  async function startCheckout(slug: string): Promise<{ url: string; resumed?: boolean }> {
    return await api<{ url: string; resumed?: boolean }>('/billing/checkout/', {
      method: 'POST',
      body: { slug },
    })
  }

  // fetchSession is called directly by /billing/success's own poll loop, not
  // wrapped here, matching the useRuns.ts precedent of a composable exposing a
  // fetch function that the page schedules on an interval. It must re-throw so
  // the page can distinguish a terminal 403/404 from a transient 502/503 or
  // network failure — swallowing the error here would make that distinction
  // impossible for the caller.
  async function fetchSession(sessionId: string): Promise<SessionStatus> {
    return await api<SessionStatus>(`/billing/session/${encodeURIComponent(sessionId)}/`)
  }

  const planChange = ref<PlanChangeState | null>(null)
  const planChangeLoading = ref(false)
  const planChangeError = ref<string | null>(null)
  // Set when GET /billing/change-plan/ answers account_locked (disputed account): the server's
  // detail. The page watches it and switches to its locked view; no plan data is kept.
  const planChangeLocked = ref<string | null>(null)

  async function fetchPlanChange(): Promise<void> {
    planChangeLoading.value = true
    planChangeError.value = null
    try {
      planChange.value = await api<PlanChangeState>('/billing/change-plan/')
    } catch (err: unknown) {
      if (isAuthError(err)) return
      if (isAccountLocked(err)) {
        planChange.value = null
        planChangeLocked.value = accountLockedMessage(err, supportEmail)
        return
      }
      const e = err as { status?: number; data?: { code?: string } }
      if (e?.status === 409 && (e.data?.code === 'exempt' || e.data?.code === 'no_active_subscription')) {
        planChange.value = null
        return
      }
      planChangeError.value = 'We couldn\'t load your plan options. Please try again.'
    } finally {
      planChangeLoading.value = false
    }
  }

  // The three below re-throw on every failure; callers branch on err.status / err.data.code
  // (account_locked via isAccountLocked) and then refetch, so the server stays the source of truth.
  async function schedulePlanChange(slug: string): Promise<{ pending: PlanChangePending | null }> {
    return await api<{ pending: PlanChangePending | null }>('/billing/change-plan/', {
      method: 'POST',
      body: { plan: slug },
    })
  }

  // DELETE returns 204 with no body; ofetch resolves that to undefined.
  async function cancelPlanChange(): Promise<void> {
    await api('/billing/change-plan/', { method: 'DELETE' })
  }

  // Click-only: the Stripe session URL is short-lived, so never call on mount or hover.
  // Re-throws; a disputed account answers 409 account_locked (see isAccountLocked).
  async function openBillingPortal(): Promise<{ url: string }> {
    return await api<{ url: string }>('/billing/portal/', { method: 'POST' })
  }

  return {
    plans,
    plansLoading,
    plansError,
    fetchPlans,
    startCheckout,
    fetchSession,
    planChange,
    planChangeLoading,
    planChangeError,
    planChangeLocked,
    fetchPlanChange,
    schedulePlanChange,
    cancelPlanChange,
    openBillingPortal,
  }
}
