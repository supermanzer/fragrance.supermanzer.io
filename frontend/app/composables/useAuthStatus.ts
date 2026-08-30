import { useApi, clearTokens, isAuthError } from '~/composables/useApi'

// 'unknown' carries two meanings by design: "haven't checked yet" (initial
// state) and "checked, but couldn't confirm either way" (a verify/refresh call
// came back indeterminate — network blip, 429, etc). Collapsing both into one
// state is what lets checkAuth()'s early-return guard below double as a
// retry-on-next-mount for the indeterminate case, with no separate mechanism
// needed. CTA consumers (index.vue, public.vue) treat 'unknown' the same as
// 'unauthenticated' — defaulting a link to '/auth/login' when we're not sure
// is safe, worst case an authenticated user re-visits login. login.vue is
// the one place this is NOT safe to lean on directly: that page's job is
// letting people sign in, so it must not treat an indeterminate, possibly
// permanent 'unknown' as a reason to keep showing a spinner instead of the
// form. It deliberately gates on its own local "check in flight" ref rather
// than on authStatus === 'unknown' — see login.vue for the reasoning.
export type AuthStatus = 'unknown' | 'authenticated' | 'unauthenticated'

const TOKEN_KEY = 'auth_access'

// Module-scoped singleton: every caller of useAuthStatus() across the app (index.vue,
// the public layout's nav bar, login.vue) shares this one ref, so the background
// verify/refresh check runs at most once per page load and every consumer reacts to
// the same resolved value. checkAuth() only ever *writes* to this ref behind an
// `import.meta.client` guard, so on the server it stays at its initial 'unknown' for
// every request — there is no cross-request state leak in the Node process, and no
// hydration mismatch on the client (server always renders the 'unknown'/default branch).
const authStatus = ref<AuthStatus>('unknown')
let inflight: Promise<void> | null = null

export function useAuthStatus() {
  const config = useRuntimeConfig()
  const { refreshTokens } = useApi()

  // The verify endpoint only documents 200 (valid) and 401 (invalid/expired)
  // responses. A 401 is a confirmed-invalid access token, worth falling
  // through to a refresh attempt, same as before. Anything else (network
  // failure, 429, 5xx) is indeterminate — NOT grounds to assume the token is
  // invalid, and NOT grounds to cascade into treating a follow-up refresh
  // failure as confirmed-invalid either.
  async function verifyToken(token: string): Promise<'valid' | 'invalid' | 'indeterminate'> {
    try {
      await $fetch('/auth/token/verify/', {
        baseURL: config.public.apiBase,
        method: 'POST',
        body: { token },
      })
      return 'valid'
    } catch (err) {
      return isAuthError(err) ? 'invalid' : 'indeterminate'
    }
  }

  async function runCheck(): Promise<void> {
    const token = localStorage.getItem(TOKEN_KEY)
    if (!token) {
      authStatus.value = 'unauthenticated'
      return
    }

    const verifyResult = await verifyToken(token)
    if (verifyResult === 'valid') {
      authStatus.value = 'authenticated'
      return
    }
    if (verifyResult === 'indeterminate') {
      // Couldn't confirm the access token is invalid — leave authStatus at
      // 'unknown' rather than asserting a state we don't actually know.
      // checkAuth()'s early-return guard only re-runs this check while
      // authStatus is 'unknown', so the next page mount retries naturally.
      return
    }

    // verifyResult === 'invalid': access token confirmed invalid/expired.
    const refreshResult = await refreshTokens()
    if (refreshResult === 'refreshed') {
      authStatus.value = 'authenticated'
    } else if (refreshResult === 'invalid') {
      clearTokens()
      authStatus.value = 'unauthenticated'
    }
    // refreshResult === 'indeterminate': same reasoning as above — leave
    // authStatus at 'unknown' instead of force-logging out on a transient error.
  }

  async function checkAuth(): Promise<void> {
    if (!import.meta.client) return
    // Only re-check on a genuinely fresh page load (or after an indeterminate
    // result left authStatus at 'unknown'). Once resolved to 'authenticated'
    // or 'unauthenticated', re-running on every SPA navigation would hit the
    // shared nginx auth rate-limit zone for no benefit — this app has no
    // current need to re-verify mid-session.
    if (authStatus.value !== 'unknown') return
    if (!inflight) {
      inflight = runCheck().finally(() => {
        inflight = null
      })
    }
    await inflight
  }

  return { authStatus, checkAuth }
}
