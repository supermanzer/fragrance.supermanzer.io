<template>
  <div style="max-width: 480px; margin: 0 auto" class="text-center py-16">
    <VFadeTransition mode="out-in">
      <div v-if="sessionNotFound" key="not-found">
        <v-icon icon="mdi-help-circle-outline" size="48" class="text-medium-emphasis mb-4" />
        <p class="text-h6 font-weight-regular mb-2">We couldn't find that checkout session</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          This page needs a valid checkout session to check on. If you just paid, check your billing
          page to see your subscription status.
        </p>
        <v-btn color="primary" variant="flat" rounded="lg" to="/billing">Go to billing</v-btn>
      </div>

      <div v-else-if="lockedMessage" key="locked">
        <v-alert type="warning" variant="tonal" class="mb-6 text-left">
          <p class="text-h6 font-weight-regular mb-2">Access on this account is paused</p>
          <p class="text-body-2">{{ lockedMessage }}</p>
        </v-alert>
        <v-btn color="primary" variant="flat" rounded="lg" to="/billing">Go to billing</v-btn>
      </div>

      <div v-else-if="resolved" key="resolved">
        <v-icon icon="mdi-check-circle-outline" size="48" color="success" class="mb-4" />
        <p class="text-h6 font-weight-regular mb-2">You're in.</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          Your subscription is active. Head to your collection to start your first recommendation run.
        </p>
        <p v-if="plan" class="text-caption text-medium-emphasis mb-6">
          Subscribed to the {{ plan.name }} plan.
        </p>
        <v-btn color="primary" variant="flat" rounded="lg" to="/fragrance">Go to your collection</v-btn>
      </div>

      <div v-else-if="revoked" key="revoked">
        <v-alert type="warning" variant="tonal" class="mb-6 text-left">
          <p class="text-h6 font-weight-regular mb-2">Access on this account is paused</p>
          <p class="text-body-2">
            A payment dispute was opened on this account. Your billing page has the details. {{ lockedSentence }}
          </p>
        </v-alert>
        <v-btn color="primary" variant="flat" rounded="lg" to="/billing">Go to billing</v-btn>
      </div>

      <div v-else-if="confirming" key="confirming">
        <v-progress-circular indeterminate color="primary" size="48" width="3" class="mb-4" />
        <p class="text-h6 font-weight-regular mb-2">Confirming your payment</p>
        <p class="text-body-2 text-medium-emphasis">This usually takes just a few seconds.</p>
      </div>

      <div v-else-if="stillProcessing" key="still-processing">
        <v-progress-circular indeterminate color="primary" size="48" width="3" class="mb-4" />
        <p class="text-h6 font-weight-regular mb-2">Just a moment</p>
        <p class="text-body-2 text-medium-emphasis">
          Stripe has your payment. We're waiting on the final confirmation, which can take a minute
          to arrive. This page updates on its own, no need to refresh or pay again.
        </p>
      </div>

      <div v-else key="give-up">
        <v-icon icon="mdi-clock-outline" size="48" class="text-medium-emphasis mb-4" />
        <p class="text-h6 font-weight-regular mb-2">This is taking longer than usual</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          Your payment went through, and you have not been charged twice. Stripe's confirmation is
          just slower than usual this time. Check back in a few minutes, or refresh this page, most
          delays clear on their own.
        </p>
        <v-btn variant="flat" color="primary" rounded="lg" class="mb-4" @click="restartPolling">
          Refresh
        </v-btn>
        <div>
          <NuxtLink to="/" class="text-body-2 text-primary">Return home</NuxtLink>
        </div>
      </div>
    </VFadeTransition>
  </div>
</template>

<script setup lang="ts">
import { isAccountLocked, accountLockedMessage } from '~/composables/useBilling'

definePageMeta({ layout: 'default' })

const POLL_INTERVAL_MS = 5000
const MAX_POLLS = 18
const STILL_PROCESSING_THRESHOLD = 4

const route = useRoute()
const supportEmail = useSupportEmail()
const lockedSentence = lockedRestartSentence(supportEmail)
const { fetchSession } = useBilling()
const { refreshSubscription } = useSubscription()

const sessionId = computed(() => {
  const raw = route.query.session_id
  return typeof raw === 'string' ? raw : ''
})

const status = ref<'none' | 'checkout_started' | 'active' | 'revoked'>('none')
const plan = ref<{ slug: string; name: string } | null>(null)
const hasAccess = ref(false)
const pollCount = ref(0)
const notFoundError = ref(false)
// The server's account_locked detail (disputed account). Terminal: polling stops and this is
// shown instead of the checkout outcome.
const lockedMessage = ref<string | null>(null)
const pollTimer = ref<ReturnType<typeof setTimeout> | null>(null)

const sessionNotFound = computed(() => !sessionId.value || notFoundError.value)
// Keyed on the server's access verdict, not status: a lapsed subscriber keeps status 'active'.
const resolved = computed(() => hasAccess.value)
const revoked = computed(() => status.value === 'revoked')

// Once fulfilment is confirmed, refresh the shared state so the layout banner is truthful
// by the time the user leaves this page.
watch(resolved, (isResolved) => {
  if (isResolved) refreshSubscription()
})
const confirming = computed(
  () =>
    !sessionNotFound.value &&
    !lockedMessage.value &&
    !resolved.value &&
    !revoked.value &&
    pollCount.value < STILL_PROCESSING_THRESHOLD
)
const stillProcessing = computed(
  () =>
    !sessionNotFound.value &&
    !lockedMessage.value &&
    !resolved.value &&
    !revoked.value &&
    pollCount.value >= STILL_PROCESSING_THRESHOLD &&
    pollCount.value < MAX_POLLS
)

function clearTimer(): void {
  if (pollTimer.value !== null) {
    clearTimeout(pollTimer.value)
    pollTimer.value = null
  }
}

// Chained setTimeout rather than setInterval: this endpoint makes one
// synchronous outbound Stripe call (checkout.sessions.retrieve) per hit, and none
// once access is granted, on a 1vCPU droplet, and an
// interval would let requests stack up if a single call takes longer than
// the 5s cadence. Each attempt only schedules the next one after it resolves
// (or is treated as resolved-but-inconclusive on a transient failure).
async function poll(): Promise<void> {
  if (!sessionId.value) {
    notFoundError.value = true
    return
  }

  pollCount.value++
  try {
    const data = await fetchSession(sessionId.value)
    status.value = data.status
    plan.value = data.plan
    hasAccess.value = data.has_active_access
  } catch (err: any) {
    if (isAccountLocked(err)) {
      // Terminal, not transient and not a missing session: stop polling and say so.
      lockedMessage.value = accountLockedMessage(err, supportEmail)
      void refreshSubscription()
      return
    }
    if (err?.status === 404 || err?.status === 403) {
      notFoundError.value = true
      return
    }
    // Transient failure (network error, 500, 502, 503 — Stripe unreachable or
    // unconfigured, not "session invalid"): keep the current Confirming/
    // Still-processing rendering and let the next scheduled attempt fire.
  }

  // The component may have unmounted while fetchSession was in flight — at
  // that point pollTimer.value holds an id that has already fired, so
  // clearTimeout(pollTimer.value) in clearTimer() is a no-op against it. Without
  // this guard, execution would resume after the await and schedule a fresh
  // setTimeout on a dead component, polling (and re-running Stripe fulfillment)
  // up to the 18-attempt bound with nobody watching.
  if (cancelled) return

  if (hasAccess.value || status.value === 'revoked') return
  if (pollCount.value >= MAX_POLLS) return
  pollTimer.value = setTimeout(poll, POLL_INTERVAL_MS)
}

function restartPolling(): void {
  clearTimer()
  pollCount.value = 0
  status.value = 'none'
  hasAccess.value = false
  notFoundError.value = false
  lockedMessage.value = null
  void poll()
}

let cancelled = false

onMounted(() => {
  void poll()
})

onUnmounted(() => {
  cancelled = true
  clearTimer()
})
</script>
