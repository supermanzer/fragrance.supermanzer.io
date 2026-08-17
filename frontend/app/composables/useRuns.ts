import { useApi, isAuthError } from '~/composables/useApi'
import type { RatingAction } from '~/utils/rating'

export interface Recommendation {
  id: number
  run: number
  name: string
  house: string
  status: 'confirmed' | 'replaced'
  rationale: string
  search_source_url: string
  rating: RatingAction | null
}

export interface RateOutcome {
  outcome: 'created' | 'already_rated'
  action: RatingAction
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
  const ratingInFlight = ref<Map<number, RatingAction>>(new Map())

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

  // Patches a single pick's rating in local state directly rather than refetching —
  // a full fetchRuns() would collapse the expansion panels the user has open, same
  // reasoning documented above resendEmail's silent refetch.
  function patchRating(pickId: number, action: RatingAction): void {
    for (const run of runs.value) {
      const pick = run.picks.find(p => p.id === pickId)
      if (pick) {
        pick.rating = action
        return
      }
    }
  }

  // rateRecommendation re-throws on genuine failure (network, 400, 404) so the caller
  // can show the generic-error snackbar; a 409 conflict is not re-thrown because it is
  // not a failure from the user's perspective — the pick already has a rating, so local
  // state is patched to reflect the server's existing_action and the caller shows an
  // info snackbar instead.
  async function rateRecommendation(pickId: number, action: RatingAction): Promise<RateOutcome> {
    ratingInFlight.value.set(pickId, action)
    try {
      const data = await api<{
        id: number
        action: RatingAction
        name: string
        house: string
        already_rated?: boolean
      }>(`/recommendations/${pickId}/rate/`, { method: 'POST', body: { action } })
      patchRating(pickId, data.action)
      return { outcome: data.already_rated ? 'already_rated' : 'created', action: data.action }
    } catch (err: unknown) {
      const existingAction = (err as any)?.data?.existing_action as RatingAction | undefined
      if (existingAction) {
        patchRating(pickId, existingAction)
        return { outcome: 'already_rated', action: existingAction }
      }
      throw err
    } finally {
      ratingInFlight.value.delete(pickId)
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
    ratingInFlight,
    fetchRuns,
    triggerRun,
    resendEmail,
    rateRecommendation,
    stopPolling,
  }
}
