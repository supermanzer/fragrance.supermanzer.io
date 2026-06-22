import { useApi, isAuthError } from '~/composables/useApi'

export interface Recommendation {
  id: number
  run: number
  name: string
  house: string
  status: 'confirmed' | 'replaced'
  rationale: string
  search_source_url: string
}

export interface RecommendationRun {
  id: number
  triggered_at: string
  profile: number | null
  email_html: string
  sent_at: string | null
  status: 'pending' | 'running' | 'done' | 'failed'
  celery_task_id: string
  error_message: string
  intro: string
  picks: Recommendation[]
}

const POLL_INTERVAL_MS = 20_000
const MAX_POLLS = 15  // ~5 minutes before giving up on a stuck run

export function useRuns() {
  const { api } = useApi()
  const runs = ref<RecommendationRun[]>([])
  const loading = ref(false)
  const triggering = ref(false)
  const error = ref<string | null>(null)
  const polling = ref<ReturnType<typeof setInterval> | null>(null)
  const pollCount = ref(0)

  function stopPolling(): void {
    if (polling.value !== null) {
      clearInterval(polling.value)
      polling.value = null
      pollCount.value = 0
    }
  }

  function startPolling(): void {
    if (polling.value !== null) return
    polling.value = setInterval(async () => {
      pollCount.value++
      if (pollCount.value >= MAX_POLLS) {
        stopPolling()
        return
      }
      await fetchRuns(true)
    }, POLL_INTERVAL_MS)
  }

  async function fetchRuns(silent = false): Promise<void> {
    if (!silent) loading.value = true
    if (!silent) error.value = null
    try {
      runs.value = await api<RecommendationRun[]>('/runs/')
      const hasActiveRun = runs.value.some(r => r.status === 'pending' || r.status === 'running')
      if (hasActiveRun) {
        startPolling()
      } else {
        stopPolling()
      }
    } catch (err: unknown) {
      if (isAuthError(err)) return
      if (!silent) error.value = (err as any)?.data?.detail ?? 'Failed to load runs.'
    } finally {
      if (!silent) loading.value = false
    }
  }

  async function triggerRun(): Promise<{ run_id: number }> {
    triggering.value = true
    try {
      return await api<{ run_id: number }>('/runs/trigger/', { method: 'POST' })
    } finally {
      triggering.value = false
    }
  }

  onUnmounted(stopPolling)

  return { runs, loading, triggering, error, fetchRuns, triggerRun, stopPolling }
}
