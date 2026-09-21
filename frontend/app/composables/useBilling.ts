import { useApi, isAuthError } from '~/composables/useApi'

export interface Plan {
  slug: string
  name: string
  amount_cents: number
  currency: string
  billing_interval: string
}

export interface SessionStatus {
  status: 'none' | 'checkout_started' | 'active' | 'revoked'
  plan: { slug: string; name: string } | null
  access_expires_at: string | null
  granted: boolean
}

export function useBilling() {
  const { api } = useApi()
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
  async function startCheckout(slug: string): Promise<{ url: string }> {
    return await api<{ url: string }>('/billing/checkout/', {
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
    return await api<SessionStatus>(`/billing/session/${sessionId}/`)
  }

  return { plans, plansLoading, plansError, fetchPlans, startCheckout, fetchSession }
}
