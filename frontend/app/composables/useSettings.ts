import { useApi, isAuthError } from '~/composables/useApi'

export interface FragranceConfig {
  id: number
  recipient_email: string
  gmail_user: string
  frequency: 'weekly' | 'monthly' | 'yearly'
  run_hour: number
  run_day_of_week: number
  run_day_of_month: number
  run_month: number
}

export type FragranceConfigInput = Omit<FragranceConfig, 'id'> & {
  gmail_app_password_enc?: string
}

export function useSettings() {
  const { api } = useApi()
  const config = ref<FragranceConfig | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchConfig() {
    loading.value = true
    error.value = null
    try {
      config.value = await api<FragranceConfig>('/config/')
    } catch (err: unknown) {
      if (isAuthError(err)) return
      error.value = (err as any)?.data?.detail ?? 'Failed to load settings.'
    } finally {
      loading.value = false
    }
  }

  async function updateConfig(data: Partial<FragranceConfigInput>): Promise<FragranceConfig> {
    const result = await api<FragranceConfig>('/config/', { method: 'PATCH', body: data })
    config.value = result
    return result
  }

  return { config, loading, error, fetchConfig, updateConfig }
}
