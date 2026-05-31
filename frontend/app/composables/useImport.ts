import { useApi } from '~/composables/useApi'

export interface ImportResult {
  created: number
  updated: number
  skipped: number
}

export function useImport() {
  const { api } = useApi()
  const loading = ref(false)
  const result = ref<ImportResult | null>(null)
  const error = ref<string | null>(null)

  async function importCollection(file: File): Promise<void> {
    loading.value = true
    result.value = null
    error.value = null
    try {
      const form = new FormData()
      form.append('file', file)
      result.value = await api<ImportResult>('/import/', { method: 'POST', body: form })
    } catch (err: any) {
      error.value = err?.data?.detail ?? 'Import failed. Please check the file and try again.'
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    result.value = null
    error.value = null
  }

  return { loading, result, error, importCollection, reset }
}
