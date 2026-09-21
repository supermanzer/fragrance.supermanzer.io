import type { Subscription } from '~/composables/useSubscription'

// Every useState key that holds per-user data. Kept in its own file, keyed by
// string, so useApi can reset it without importing useSubscription (which imports
// useApi). Call useSessionReset() during setup, then invoke the returned function
// wherever tokens are cleared: useState needs the Nuxt context, which is lost
// after an await.
export function useSessionReset(): () => void {
  const subscription = useState<Subscription | null>('subscription', () => null)
  const loading = useState<boolean>('subscription-loading', () => false)
  const error = useState<string | null>('subscription-error', () => null)
  const stale = useState<number>('subscription-stale', () => 0)
  const epoch = useState<number>('session-epoch', () => 0)

  return () => {
    // Bumping the epoch makes any subscription fetch still in flight discard its result.
    epoch.value++
    subscription.value = null
    loading.value = false
    error.value = null
    stale.value = 0
  }
}
