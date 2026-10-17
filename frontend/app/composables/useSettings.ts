import { useApi, isAuthError } from '~/composables/useApi'

export interface FragranceConfig {
  id: number
  // Read-only: always the verified delivery address. Changes go through useRecipient('delivery').
  recipient_email: string
  pending_recipient_email: string | null
  pending_state: 'none' | 'pending' | 'expired'
  pending_expires_at: string | null
  resend_available_at: string | null
  gmail_user: string
  frequency: 'weekly' | 'monthly' | 'yearly'
  run_hour: number
  run_day_of_week: number
  run_day_of_month: number
  run_month: number
}

// Reads may still return legacy 'weekly' / 'yearly'; writes accept only 'monthly'.
export type FragranceConfigInput = Omit<
  FragranceConfig,
  | 'id'
  | 'frequency'
  | 'recipient_email'
  | 'pending_recipient_email'
  | 'pending_state'
  | 'pending_expires_at'
  | 'resend_available_at'
> & {
  frequency: 'monthly'
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
