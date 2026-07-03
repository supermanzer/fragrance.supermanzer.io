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
  email_status: 'pending' | 'sent' | 'failed' | null
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
  const resending = ref<Set<number>>(new Set())

  const hasActiveRun = computed(() =>
    runs.value.some(
      r => r.status === 'pending' || r.status === 'running' || r.email_status === 'pending'
    )
  )

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
      const shouldPoll = runs.value.some(
        r => r.status === 'pending' || r.status === 'running' || r.email_status === 'pending'
      )
      if (shouldPoll) {
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

  // resendEmail re-throws on failure so the caller (handleResend in the page)
  // can distinguish success from failure to choose the correct snackbar color.
  async function resendEmail(runId: number): Promise<void> {
    resending.value.add(runId)
    try {
      await api(`/runs/${runId}/resend_email/`, { method: 'POST' })
      // Silent fetch: avoids collapsing the panel list to a skeleton mid-interaction.
      await fetchRuns(true)
    } finally {
      resending.value.delete(runId)
    }
  }

  onUnmounted(stopPolling)

  return {
    runs,
    loading,
    triggering,
    error,
    hasActiveRun,
    resending,
    fetchRuns,
    triggerRun,
    resendEmail,
    stopPolling,
  }
}
