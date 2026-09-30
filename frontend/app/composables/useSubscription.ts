import { useApi, isAuthError } from '~/composables/useApi'

export interface SubscriptionQuota {
  // null means unlimited (exempt account); 0 means lapsed.
  runs_allowed: number | null
  runs_used: number
  next_eligible_at: string | null
  can_trigger: boolean
  // True only for a non-staff user's one first run; the only gate for "Get my first picks".
  first_run_available: boolean
  next_run_at: string | null
}

export interface PaymentRecovery {
  state: 'retrying' | 'failed'
  attempted_at: string
}

export interface Subscription {
  status: 'none' | 'checkout_started' | 'active' | 'revoked'
  plan: { slug: string; name: string } | null
  access_expires_at: string | null
  has_active_access: boolean
  exempt: boolean
  // Strictly staff; a non-staff superuser is exempt but not is_staff.
  is_staff: boolean
  // Currently always null server-side: never claim a reason.
  lapse_reason: 'payment_failed' | 'cancelled' | null
  payment_recovery: PaymentRecovery | null
  // True whenever a billing account exists, lapsed customers included; gate UI on active state too.
  can_manage_billing: boolean
  quota: SubscriptionQuota
}

export type LapsedKind = 'never' | 'lapsed' | 'revoked'

// Client-only (fetches run in onMounted / user actions, never during SSR), so a
// module-level slot cannot leak between requests. Tagged with the session epoch so a
// fetch begun before logout is never shared with, or applied to, the next session.
let inFlight: { epoch: number; promise: Promise<void> } | null = null
let queued: { epoch: number; promise: Promise<void> } | null = null

export function useSubscription() {
  const { api } = useApi()
  const subscription = useState<Subscription | null>('subscription', () => null)
  const loading = useState<boolean>('subscription-loading', () => false)
  const error = useState<string | null>('subscription-error', () => null)
  const epoch = useState<number>('session-epoch', () => 0)

  const known = computed(() => subscription.value !== null)
  const hasAccess = computed(() => !known.value || subscription.value!.has_active_access)
  // Every lapsed surface keys on this alone. Never branch on `status` first:
  // "none" is the normal, permanent value for exempt (staff) accounts.
  const isLapsed = computed(() => {
    const sub = subscription.value
    return sub !== null && !sub.has_active_access && !sub.exempt
  })
  const lapsedKind = computed<LapsedKind | null>(() => {
    if (!isLapsed.value) return null
    const status = subscription.value?.status
    if (status === 'revoked') return 'revoked'
    if (status === 'active') return 'lapsed'
    return 'never'
  })
  const quota = computed<SubscriptionQuota | null>(() => subscription.value?.quota ?? null)
  const isExempt = computed(() => subscription.value?.exempt === true)
  // Fail-closed: false while the subscription is unknown.
  const isStaff = computed(() => subscription.value?.is_staff === true)
  const canManageBilling = computed(() => subscription.value?.can_manage_billing === true && !isExempt.value)
  const unlimited = computed(() => quota.value !== null && quota.value.runs_allowed === null)

  function startFetch(): Promise<void> {
    const startedIn = epoch.value
    loading.value = true
    error.value = null
    const promise = (async () => {
      try {
        const result = await api<Subscription>('/billing/subscription/')
        if (epoch.value === startedIn) subscription.value = result
      } catch (err: unknown) {
        if (isAuthError(err) || epoch.value !== startedIn) return
        error.value = 'Failed to load subscription status.'
      } finally {
        if (inFlight?.promise === promise) inFlight = null
        if (epoch.value === startedIn) loading.value = false
      }
    })()
    inFlight = { epoch: startedIn, promise }
    return promise
  }

  // Mount-time loads: join a request already in flight instead of firing another.
  // Resolves when data is in state, so no caller is left waiting on nothing.
  function fetchSubscription(): Promise<void> {
    if (inFlight && inFlight.epoch === epoch.value) return inFlight.promise
    return startFetch()
  }

  // Forced refetch after something changed server-side (402, 409, checkout, trigger).
  // A request already in flight may pre-date that change, so never join it: wait for it
  // to settle, then fetch once more. Concurrent forced calls share that one follow-up.
  function refreshSubscription(): Promise<void> {
    if (!inFlight || inFlight.epoch !== epoch.value) return startFetch()
    if (queued && queued.epoch === epoch.value) return queued.promise
    const startedIn = epoch.value
    const promise: Promise<void> = inFlight.promise.then(() => {
      if (queued?.promise === promise) queued = null
      return epoch.value === startedIn ? startFetch() : undefined
    })
    queued = { epoch: startedIn, promise }
    return promise
  }

  return {
    subscription,
    loading,
    error,
    fetchSubscription,
    refreshSubscription,
    known,
    hasAccess,
    isLapsed,
    lapsedKind,
    quota,
    isExempt,
    isStaff,
    canManageBilling,
    unlimited,
  }
}
