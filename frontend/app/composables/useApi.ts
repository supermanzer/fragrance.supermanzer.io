import type { FetchOptions } from 'ofetch'

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

// Three-way outcome for a refresh attempt, distinguishing "confirmed invalid"
// (safe to clear tokens and force login) from "indeterminate" (a network blip,
// a 429 from nginx throttling, etc. — NOT grounds to log a user out).
export type RefreshOutcome = 'refreshed' | 'invalid' | 'indeterminate'

function isRefreshRejected(err: unknown): boolean {
  const status = (err as { status?: number })?.status
  // SimpleJWT returns 401 for an invalid/expired/blacklisted refresh token on
  // /token/refresh/ (confirmed empirically against the running backend — see
  // .claude/security-reports/2026-08-20-auth-verify-refresh-flow.md, Finding 4).
  // 400 is included defensively: the same report observed SimpleJWT's verify
  // endpoint return 400 for a blacklisted token; treating it identically here
  // costs nothing if /token/refresh/ itself never actually produces it.
  return status === 401 || status === 400
}

interface RefreshResponse {
  access: string
  refresh?: string
}

export function useApi() {
  const config = useRuntimeConfig()

  // _fetch injects the current access token on every request. Because onRequest
  // re-reads localStorage each call, the retry below automatically picks up the
  // refreshed token without any extra wiring.
  const _fetch = $fetch.create({
    baseURL: config.public.apiBase,
    onRequest({ options }) {
      const token = getToken()
      if (token) {
        const headers = new Headers(options.headers as HeadersInit | undefined)
        headers.set('Authorization', `Bearer ${token}`)
        options.headers = headers
      }
    },
  })

  // Backend has ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION enabled, so every
  // refresh call returns a NEW refresh token and blacklists the old one server-side.
  // Both tokens from the response must be persisted, or the next refresh attempt
  // uses an already-blacklisted refresh token and fails permanently.
  async function refreshTokens(): Promise<RefreshOutcome> {
    const refresh = getRefreshToken()
    if (!refresh) return 'invalid'

    try {
      const data = await $fetch<RefreshResponse>('/auth/token/refresh/', {
        baseURL: config.public.apiBase,
        method: 'POST',
        body: { refresh },
      })
      setTokens(data.access, data.refresh ?? refresh)
      return 'refreshed'
    } catch (err) {
      if (getRefreshToken() !== refresh) {
        // A concurrent refresh call (e.g. useAuthStatus.checkAuth() racing this
        // call's own 401-retry, or a second browser tab sharing localStorage)
        // already rotated this token successfully before we got our response.
        // Our copy is stale by design, not because the session is invalid —
        // report success rather than clobbering the winner's fresh tokens.
        return 'refreshed'
      }
      return isRefreshRejected(err) ? 'invalid' : 'indeterminate'
    }
  }

  async function api<T = unknown>(
    url: string,
    options?: FetchOptions,
  ): Promise<T> {
    try {
      return await _fetch<T>(url, options)
    } catch (err) {
      if (!isAuthError(err)) throw err

      const refreshResult = await refreshTokens()
      if (refreshResult === 'invalid') {
        clearTokens()
        await navigateTo('/auth/login')
        throw err
      }
      if (refreshResult === 'indeterminate') {
        // Couldn't confirm the refresh failed for a real auth reason (network
        // blip, 429 from nginx throttling, etc). Don't clear tokens or force a
        // logout — surface the original error like any other transient failure.
        throw err
      }

      return await _fetch<T>(url, options)
    }
  }

  return { api, refreshTokens }
}
