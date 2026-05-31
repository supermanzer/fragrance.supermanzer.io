import { useApi, isAuthError } from '~/composables/useApi'

export interface PreferenceProfile {
  id: number
  generated_at: string
  loved_notes: string
  liked_notes: string
  disliked_notes: string
  owns_list: string[]
  search_angle_1: string
  search_angle_2: string
}

export function useProfile() {
  const { api } = useApi()
  const profile = ref<PreferenceProfile | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchProfile() {
    loading.value = true
    error.value = null
    try {
      profile.value = await api<PreferenceProfile>('/profile/')
    } catch (err: unknown) {
      if (isAuthError(err)) return
      if ((err as any)?.status === 404) {
        profile.value = null
      } else {
        error.value = (err as any)?.data?.detail ?? 'Failed to load profile.'
      }
    } finally {
      loading.value = false
    }
  }

  return { profile, loading, error, fetchProfile }
}
