import { useApi, isAuthError } from '~/composables/useApi'

export interface Subscription {
  status: 'none' | 'checkout_started' | 'active' | 'revoked'
  plan: { slug: string; name: string } | null
  access_expires_at: string | null
  has_active_access: boolean
}

export function useSubscription() {
  const { api } = useApi()
  const subscription = ref<Subscription | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchSubscription(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      subscription.value = await api<Subscription>('/billing/subscription/')
    } catch (err: unknown) {
      if (isAuthError(err)) return
      error.value = 'Failed to load subscription status.'
    } finally {
      loading.value = false
    }
  }

  return { subscription, loading, error, fetchSubscription }
}
