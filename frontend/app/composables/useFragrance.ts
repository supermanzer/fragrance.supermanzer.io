import { useApi, isAuthError } from '~/composables/useApi'

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
  const loading = ref(true)
  const hasLoaded = ref(false)
  const error = ref<string | null>(null)
  let requestSeq = 0

  async function fetchFragrances(status?: Fragrance['status']) {
    const seq = ++requestSeq
    loading.value = true
    error.value = null
    try {
      const query = status ? `?status=${encodeURIComponent(status)}` : ''
      const result = await api<Fragrance[]>(`/collection/${query}`)
      if (seq !== requestSeq) return
      fragrances.value = result
    } catch (err: unknown) {
      if (seq !== requestSeq) return
      if (isAuthError(err)) return
      error.value = (err as any)?.data?.detail ?? 'Failed to load fragrances.'
    } finally {
      if (seq === requestSeq) {
        loading.value = false
        hasLoaded.value = true
      }
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

  function invalidatePending(): void {
    requestSeq++
  }

  return { fragrances, loading, hasLoaded, error, fetchFragrances, createFragrance, updateFragrance, deleteFragrance, invalidatePending }
}
