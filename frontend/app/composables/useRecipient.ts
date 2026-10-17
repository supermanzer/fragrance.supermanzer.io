import { useApi, isAuthError, isSubscriptionRequired } from '~/composables/useApi'

// One state machine for both addresses that go through "request, then confirm by link":
// 'delivery' (where picks and notices go; /config/recipient/*) and 'receipt' (where Stripe
// receipts go; /billing/receipt-email/*). Both share the verified/pending/expired shape, the
// cooldown, the error mapping and the current-password check. State is local to each call, so
// it cannot leak between signed-in users.
export type RecipientPurpose = 'delivery' | 'receipt'
export type PendingState = 'none' | 'pending' | 'expired'
export type RecipientBlock = 'exempt' | 'account_locked'

interface Endpoints {
  read: string
  request: string
  resend: string
  cancel: string
}

const ENDPOINTS: Record<RecipientPurpose, Endpoints> = {
  delivery: {
    read: '/config/',
    request: '/config/recipient/',
    resend: '/config/recipient/resend/',
    cancel: '/config/recipient/pending/',
  },
  receipt: {
    read: '/billing/receipt-email/',
    request: '/billing/receipt-email/',
    resend: '/billing/receipt-email/resend/',
    cancel: '/billing/receipt-email/pending/',
  },
}

// Fields common to GET /config/, GET /billing/receipt-email/ and every 202 body.
interface PendingFields {
  pending_state?: PendingState
  pending_expires_at?: string | null
  resend_available_at?: string | null
  pending_recipient_email?: string | null
  pending_receipt_email?: string | null
  recipient_email?: string
  receipt_email?: string | null
  has_customer?: boolean
}

interface ErrorInfo {
  status: number | null
  code: string | null
  retryAfter: number | null
}

export const SEND_FAILED_MESSAGE = "We couldn't send the link just now. Please try again in a moment."
export const CANCEL_FAILED_MESSAGE = "We couldn't cancel the change just now. Please try again in a moment."
export const EMAIL_REQUIRED_MESSAGE = 'Enter an email address.'
export const EMAIL_MALFORMED_MESSAGE = "That doesn't look like an email address."
export const PASSWORD_REQUIRED_MESSAGE = 'Enter your current password.'
export const PASSWORD_WRONG_MESSAGE = "That password isn't right."

// Same coarse shape check the browser applies to type="email"; the server is the real validator.
const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

function errorInfo(err: unknown): ErrorInfo {
  const e = err as { status?: unknown; data?: { code?: unknown; retry_after?: unknown } } | null
  const retry = e?.data?.retry_after
  return {
    status: typeof e?.status === 'number' ? e.status : null,
    code: typeof e?.data?.code === 'string' ? e.data.code : null,
    retryAfter: typeof retry === 'number' && Number.isFinite(retry) ? retry : null,
  }
}

// "{n} minutes", n = retry_after seconds rounded up, minimum 1.
export function rateLimitedMessage(retryAfter: number | null): string {
  const minutes = Math.max(1, Math.ceil((retryAfter ?? 0) / 60))
  const unit = minutes === 1 ? 'minute' : 'minutes'
  return `You've requested a few links in a short time. Please try again in ${minutes} ${unit}.`
}

const COOLDOWN_CAP_MS = 60_000
const FEEDBACK_MS = 6000

export function useRecipient(purpose: RecipientPurpose) {
  const { api } = useApi()
  const endpoints = ENDPOINTS[purpose]
  const noun = purpose === 'delivery' ? 'Picks' : 'Receipts'

  const verified = ref<string | null>(null)
  const pending = ref<string | null>(null)
  const pendingState = ref<PendingState>('none')
  const pendingExpiresAt = ref<string | null>(null)
  // The delivery address always exists (blank falls back to the account email server-side).
  const hasCustomer = ref(true)
  const blocked = ref<RecipientBlock | null>(null)

  const loaded = ref(false)
  const loading = ref(false)
  const loadFailed = ref(false)
  const submitting = ref(false)
  const resending = ref(false)
  const cancelling = ref(false)

  const emailError = ref<string | null>(null)
  const passwordError = ref<string | null>(null)
  const actionError = ref<string | null>(null)
  const subscriptionRequired = ref(false)
  const feedback = ref<string | null>(null)

  // Resend cooldown. The server sends an absolute resend_available_at; the client counts down
  // at most one minute from the moment the snapshot arrived, so a skewed device clock can never
  // lock Resend for longer than the real 60 s gap.
  const cooldownDeadline = ref<number | null>(null)
  const now = ref(0)
  let tick: ReturnType<typeof setInterval> | null = null
  let feedbackTimer: ReturnType<typeof setTimeout> | null = null

  function stopTick(): void {
    if (tick !== null) {
      clearInterval(tick)
      tick = null
    }
  }

  watch(cooldownDeadline, (deadline) => {
    stopTick()
    if (deadline === null || !import.meta.client) return
    now.value = Date.now()
    tick = setInterval(() => {
      now.value = Date.now()
      if (now.value >= deadline) stopTick()
    }, 1000)
  })

  onScopeDispose(() => {
    stopTick()
    if (feedbackTimer !== null) clearTimeout(feedbackTimer)
  })

  const cooldownSeconds = computed(() => {
    const deadline = cooldownDeadline.value
    if (deadline === null) return 0
    return Math.max(0, Math.ceil((deadline - now.value) / 1000))
  })
  const cooling = computed(() => cooldownSeconds.value > 0)

  // True when a Stripe customer exists but its address could not be read: never show a guess.
  const unknown = computed(() => purpose === 'receipt' && hasCustomer.value && loaded.value && verified.value === null)

  function armCooldown(iso: string | null | undefined): void {
    if (!iso) {
      cooldownDeadline.value = null
      return
    }
    const ms = Date.parse(iso) - Date.now()
    cooldownDeadline.value = ms > 0 ? Date.now() + Math.min(ms, COOLDOWN_CAP_MS) : null
  }

  function showFeedback(message: string): void {
    feedback.value = message
    if (feedbackTimer !== null) clearTimeout(feedbackTimer)
    feedbackTimer = setTimeout(() => {
      feedback.value = null
      feedbackTimer = null
    }, FEEDBACK_MS)
  }

  function clearErrors(): void {
    emailError.value = null
    passwordError.value = null
    actionError.value = null
    subscriptionRequired.value = false
  }

  function apply(data: PendingFields): void {
    if (purpose === 'delivery') {
      if (typeof data.recipient_email === 'string') verified.value = data.recipient_email
      pending.value = data.pending_recipient_email ?? null
    } else {
      if (typeof data.has_customer === 'boolean') hasCustomer.value = data.has_customer
      if (data.receipt_email !== undefined) verified.value = data.receipt_email
      pending.value = data.pending_receipt_email ?? null
    }
    pendingState.value = pending.value === null ? 'none' : (data.pending_state ?? 'pending')
    pendingExpiresAt.value = data.pending_expires_at ?? null
    armCooldown(pendingState.value === 'none' ? null : data.resend_available_at)
  }

  // Adopts a snapshot the caller already fetched (Settings passes GET /config/), so the page
  // makes one read instead of two. Same end state as a successful load().
  function applySnapshot(data: PendingFields): void {
    apply(data)
    blocked.value = null
    loadFailed.value = false
    loaded.value = true
  }

  // Returns false when the read failed. `silent` skips the loading flag (background refetch).
  async function load(silent = false): Promise<boolean> {
    if (!silent) loading.value = true
    loadFailed.value = false
    try {
      apply(await api<PendingFields>(endpoints.read))
      blocked.value = null
      loaded.value = true
      return true
    } catch (err: unknown) {
      if (isAuthError(err)) return false
      const info = errorInfo(err)
      if (info.status === 409 && (info.code === 'exempt' || info.code === 'account_locked')) {
        blocked.value = info.code
        loaded.value = true
        return true
      }
      if (!silent || !loaded.value) loadFailed.value = true
      return false
    } finally {
      loading.value = false
    }
  }

  // Maps a failure shared by request and resend onto state.
  async function handleCommonError(err: unknown, resend: boolean): Promise<void> {
    const info = errorInfo(err)
    if (isSubscriptionRequired(err)) {
      subscriptionRequired.value = true
      return
    }
    if (info.status === 409 && (info.code === 'exempt' || info.code === 'account_locked')) {
      blocked.value = info.code
      return
    }
    if (info.status === 409 && info.code === 'no_stripe_customer') {
      hasCustomer.value = false
      return
    }
    if (info.status === 409 && info.code === 'no_pending') {
      await load(true)
      return
    }
    if (info.status === 429 && info.code === 'resend_too_soon') {
      const seconds = Math.min(Math.max(info.retryAfter ?? 60, 1), 60)
      cooldownDeadline.value = Date.now() + seconds * 1000
      return
    }
    if (info.status === 429) {
      const message = rateLimitedMessage(info.retryAfter)
      if (resend) actionError.value = message
      else emailError.value = message
      return
    }
    if (info.status === 503 && info.code === 'send_unavailable') {
      // The change was stored but the mail did not go out: show it as pending so Resend is offered.
      await load(true)
      actionError.value = SEND_FAILED_MESSAGE
      return
    }
    actionError.value = SEND_FAILED_MESSAGE
  }

  function validate(email: string, password: string): boolean {
    emailError.value = null
    passwordError.value = null
    const trimmed = email.trim()
    if (!trimmed) emailError.value = EMAIL_REQUIRED_MESSAGE
    else if (!EMAIL_SHAPE.test(trimmed)) emailError.value = EMAIL_MALFORMED_MESSAGE
    if (!password) passwordError.value = PASSWORD_REQUIRED_MESSAGE
    return emailError.value === null && passwordError.value === null
  }

  // Resolves true when the link was sent (a pending change now exists).
  async function requestChange(email: string, currentPassword: string): Promise<boolean> {
    if (submitting.value) return false
    clearErrors()
    if (!validate(email, currentPassword)) return false
    submitting.value = true
    try {
      const data = await api<PendingFields>(endpoints.request, {
        method: 'POST',
        body: { email: email.trim(), current_password: currentPassword },
      })
      apply(data)
      showFeedback(`Confirmation link sent to ${pending.value ?? email.trim()}.`)
      return true
    } catch (err: unknown) {
      if (isAuthError(err)) return false
      const info = errorInfo(err)
      if (info.status === 400 && info.code === 'invalid_email') emailError.value = EMAIL_MALFORMED_MESSAGE
      else if (info.status === 400 && info.code === 'invalid_password') passwordError.value = PASSWORD_WRONG_MESSAGE
      else if (info.status === 400 && info.code === 'same_as_current') {
        emailError.value = `That's already where your ${noun.toLowerCase()} are sent.`
      } else await handleCommonError(err, false)
      return false
    } finally {
      submitting.value = false
    }
  }

  // Works for pending and expired changes. Resolves true when a link went out.
  async function resend(): Promise<boolean> {
    if (resending.value || cooling.value) return false
    clearErrors()
    resending.value = true
    try {
      apply(await api<PendingFields>(endpoints.resend, { method: 'POST' }))
      showFeedback(`Link sent again to ${pending.value ?? 'that address'}.`)
      return true
    } catch (err: unknown) {
      if (isAuthError(err)) return false
      await handleCommonError(err, true)
      return false
    } finally {
      resending.value = false
    }
  }

  // Idempotent and never gated. Resolves true when nothing is pending any more.
  async function cancel(): Promise<boolean> {
    if (cancelling.value) return false
    clearErrors()
    cancelling.value = true
    try {
      await api(endpoints.cancel, { method: 'DELETE' })
      pending.value = null
      pendingState.value = 'none'
      pendingExpiresAt.value = null
      cooldownDeadline.value = null
      showFeedback(`Change cancelled. ${noun} continue to ${verified.value ?? 'the current address'}.`)
      return true
    } catch (err: unknown) {
      if (isAuthError(err)) return false
      actionError.value = CANCEL_FAILED_MESSAGE
      return false
    } finally {
      cancelling.value = false
    }
  }

  return {
    purpose,
    verified,
    pending,
    pendingState,
    pendingExpiresAt,
    hasCustomer,
    blocked,
    unknown,
    loaded,
    loading,
    loadFailed,
    submitting,
    resending,
    cancelling,
    emailError,
    passwordError,
    actionError,
    subscriptionRequired,
    feedback,
    cooling,
    cooldownSeconds,
    load,
    applySnapshot,
    requestChange,
    resend,
    cancel,
    clearErrors,
  }
}
