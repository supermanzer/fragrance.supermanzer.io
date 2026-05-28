import { useApi } from '~/composables/useApi'

export interface Fragrance {
  id: number
  name: string
  house: string
  status: 'own' | 'like' | 'dislike'
  notes: string
  added_at: string
  updated_at: string
  source_recommendation: number | null
}

export type FragranceInput = Pick<Fragrance, 'name' | 'house' | 'status' | 'notes'>

export function useFragrance() {
  const { api } = useApi()
  const fragrances = ref<Fragrance[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetchFragrances(status?: string) {
    loading.value = true
    error.value = null
    try {
      const query = status ? `?status=${status}` : ''
      fragrances.value = await api<Fragrance[]>(`/collection/${query}`)
    } catch (err: any) {
      error.value = err?.data?.detail ?? 'Failed to load fragrances.'
    } finally {
      loading.value = false
    }
  }

  async function createFragrance(data: FragranceInput): Promise<Fragrance> {
    return api<Fragrance>('/collection/', { method: 'POST', body: data })
  }

  async function updateFragrance(id: number, data: Partial<FragranceInput>): Promise<Fragrance> {
    return api<Fragrance>(`/collection/${id}/`, { method: 'PATCH', body: data })
  }

  async function deleteFragrance(id: number): Promise<void> {
    await api(`/collection/${id}/`, { method: 'DELETE' })
  }

  return { fragrances, loading, error, fetchFragrances, createFragrance, updateFragrance, deleteFragrance }
}
