import { useApi } from '~/composables/useApi'

export function useExport() {
  const { api } = useApi()
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function exportCollection(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      // ofetch supports responseType: 'blob' natively, so api() works here and
      // preserves the 401-refresh retry logic from useApi. Dropping to raw $fetch
      // would lose that retry and require manual token injection.
      const blob = await api<Blob>('/export/', { responseType: 'blob' })

      // Create a temporary anchor element and programmatically click it to trigger
      // the browser's native file download dialog. This is the standard technique
      // for downloading blob content without a dedicated download frame.
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'fragrances.csv'
      anchor.click()

      // Revoke the object URL on the next tick so the browser has time to
      // initiate the download before the URL is invalidated.
      setTimeout(() => URL.revokeObjectURL(url), 100)
    } catch {
      // With responseType: 'blob', error response bodies also arrive as Blobs.
      // Reading err?.data?.detail would require an async FileReader call. A
      // generic message is the correct tradeoff here.
      error.value = 'Export failed. Please try again.'
    } finally {
      loading.value = false
    }
  }

  function reset(): void {
    error.value = null
  }

  return { loading, error, exportCollection, reset }
}
