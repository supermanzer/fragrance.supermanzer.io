const TOKEN_KEY = 'auth_access'
const REFRESH_KEY = 'auth_refresh'

function getToken(): string | null {
  if (!import.meta.client) return null
  return localStorage.getItem(TOKEN_KEY)
}

function getRefreshToken(): string | null {
  if (!import.meta.client) return null
  return localStorage.getItem(REFRESH_KEY)
}

export function setTokens(access: string, refresh: string): void {
  if (!import.meta.client) return
  localStorage.setItem(TOKEN_KEY, access)
  localStorage.setItem(REFRESH_KEY, refresh)
}

export function clearTokens(): void {
  if (!import.meta.client) return
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(REFRESH_KEY)
}

export function isAuthError(err: unknown): boolean {
  return (err as any)?.status === 401
}

export function useApi() {
  const config = useRuntimeConfig()

  const api = $fetch.create({
    baseURL: config.public.apiBase,

    onRequest({ options }) {
      const token = getToken()
      if (token) {
        const headers = new Headers(options.headers as HeadersInit | undefined)
        headers.set('Authorization', `Bearer ${token}`)
        options.headers = headers
      }
    },

    async onResponseError({ response, options }) {
      if (response.status !== 401) return

      const refresh = getRefreshToken()
      if (!refresh) {
        clearTokens()
        await navigateTo('/auth/login')
        return
      }

      try {
        const data = await $fetch<{ access: string }>('/auth/token/refresh/', {
          baseURL: config.public.apiBase,
          method: 'POST',
          body: { refresh },
        })
        setTokens(data.access, refresh)
        const headers = new Headers(options.headers as HeadersInit | undefined)
        headers.set('Authorization', `Bearer ${data.access}`)
        options.headers = headers
      } catch {
        clearTokens()
        await navigateTo('/auth/login')
      }
    },
  })

  return { api }
}
