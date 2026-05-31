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

export function useRuns() {
  const { api } = useApi()
  const runs = ref<RecommendationRun[]>([])
  const loading = ref(false)
  const triggering = ref(false)
  const error = ref<string | null>(null)

  async function fetchRuns() {
    loading.value = true
    error.value = null
    try {
      runs.value = await api<RecommendationRun[]>('/runs/')
    } catch (err: unknown) {
      if (isAuthError(err)) return
      error.value = (err as any)?.data?.detail ?? 'Failed to load runs.'
    } finally {
      loading.value = false
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

  return { runs, loading, triggering, error, fetchRuns, triggerRun }
}
