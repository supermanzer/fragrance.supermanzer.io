<template>
  <div style="max-width: 720px; margin: 0 auto">
    <VFadeTransition mode="out-in">
      <div v-if="isLoading && showSubscriptionView && !locked && !recovering" key="subscription-loading">
        <v-skeleton-loader type="heading" class="mb-2" />
        <v-skeleton-loader type="text" class="mb-8" />
        <v-skeleton-loader type="list-item-two-line" class="mb-8" />
        <v-skeleton-loader type="list-item-two-line" />
      </div>

      <div v-else-if="isLoading && !locked && !recovering" key="loading">
        <p class="text-h4 mb-2">Choose your plan</p>
        <p class="text-body-2 text-medium-emphasis mb-8">
          One plan, billed however you'd rather pay. Either way, you get one recommendation run
          every 30 days.
        </p>
        <v-row class="ga-4">
          <v-col cols="12" sm="6">
            <v-skeleton-loader type="card" />
          </v-col>
          <v-col cols="12" sm="6">
            <v-skeleton-loader type="card" />
          </v-col>
        </v-row>
      </div>

      <div v-else-if="exempt" key="exempt" class="text-center py-16">
        <v-icon icon="mdi-check-decagram-outline" size="48" color="primary" class="mb-4" />
        <p class="text-h6 font-weight-regular mb-2">No subscription needed</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          Your account has full access, so there's nothing to buy. Runs aren't limited on this account.
        </p>
        <v-btn color="primary" variant="flat" rounded="lg" to="/fragrance">Go to your collection</v-btn>
      </div>

      <div v-else-if="locked" key="locked" class="text-center py-8 py-sm-16">
        <VIcon icon="mdi-lock-outline" size="48" class="text-medium-emphasis mb-4" aria-hidden="true" />
        <p id="locked-title" tabindex="-1" class="text-h6 font-weight-regular mb-2">Your account is on hold</p>
        <p class="text-body-2 text-medium-emphasis mb-6 mx-auto" style="max-width: 480px">
          A payment dispute was opened on this account, so new subscriptions are switched off for now. Your collection and past recommendations are still here to read and export. {{ lockedSentence }}
        </p>
        <VAlert
          v-if="lockedDetail ?? portalError"
          type="error"
          variant="tonal"
          rounded="lg"
          class="mb-4 mx-auto text-left"
          style="max-width: 480px"
        >
          {{ lockedDetail ?? portalError }}
        </VAlert>
        <div class="d-flex flex-column flex-sm-row justify-center ga-3">
          <VBtn
            variant="flat"
            color="primary"
            rounded="lg"
            height="44"
            prepend-icon="mdi-email-outline"
            :href="`mailto:${supportEmail}?subject=Account%20on%20hold`"
          >
            Email support
          </VBtn>
          <VBtn
            v-if="canManageBilling"
            variant="outlined"
            color="primary"
            rounded="lg"
            height="44"
            prepend-icon="mdi-receipt-text-outline"
            :loading="portalRedirecting"
            :disabled="portalRedirecting"
            @click="manageBilling"
          >
            View invoices
          </VBtn>
          <VBtn
            variant="text"
            color="primary"
            rounded="lg"
            height="44"
            to="/fragrance"
          >
            Go to your collection
          </VBtn>
        </div>
      </div>

      <div v-else-if="recovering" key="recovering" class="text-center py-8 py-sm-16">
        <VFadeTransition mode="out-in">
          <div :key="recoveryPhase">
            <VProgressCircular
              v-if="recoveryPhase === 'checking' || recoveryPhase === 'retrying'"
              indeterminate
              color="primary"
              size="48"
              width="3"
              class="mb-4"
            />
            <VIcon
              v-else-if="recoveryPhase === 'restored'"
              icon="mdi-check-circle-outline"
              size="48"
              color="success"
              class="mb-4"
              aria-hidden="true"
            />
            <VIcon
              v-else-if="recoveryPhase === 'declined'"
              icon="mdi-credit-card-off-outline"
              size="48"
              color="warning"
              class="mb-4"
              aria-hidden="true"
            />
            <VIcon
              v-else-if="recoveryPhase === 'timed-out'"
              icon="mdi-clock-outline"
              size="48"
              class="text-medium-emphasis mb-4"
              aria-hidden="true"
            />

            <div :role="recoveryTerminal ? undefined : 'status'">
              <p id="recovery-title" tabindex="-1" class="text-h6 font-weight-regular mb-2">{{ recoveryCopy.title }}</p>
              <p class="text-body-2 text-medium-emphasis mb-6 mx-auto" style="max-width: 480px">{{ recoveryCopy.body }}</p>
            </div>

            <VAlert
              v-if="portalError"
              type="error"
              variant="tonal"
              rounded="lg"
              class="mb-4 mx-auto text-left"
              style="max-width: 480px"
            >
              {{ portalError }}
            </VAlert>

            <div class="d-flex flex-column flex-sm-row justify-center ga-3">
              <VBtn
                v-if="recoveryPhase === 'restored' || recoveryPhase === 'declined'"
                :variant="recoveryPhase === 'restored' ? 'flat' : 'text'"
                color="primary"
                rounded="lg"
                height="44"
                :class="recoveryPhase === 'declined' ? 'order-sm-2' : ''"
                to="/fragrance"
              >
                Go to your collection
              </VBtn>
              <VBtn
                v-if="recoveryPhase === 'declined'"
                variant="flat"
                color="primary"
                rounded="lg"
                height="44"
                class="order-first order-sm-1"
                prepend-icon="mdi-credit-card-outline"
                :loading="portalRedirecting"
                :disabled="portalRedirecting"
                @click="manageBilling"
              >
                Try another card
              </VBtn>
              <VBtn
                v-if="recoveryPhase === 'timed-out'"
                variant="flat"
                color="primary"
                rounded="lg"
                height="44"
                @click="checkAgain"
              >
                Check again
              </VBtn>
              <VBtn
                v-if="recoveryPhase === 'waiting' || recoveryPhase === 'timed-out'"
                variant="text"
                color="primary"
                rounded="lg"
                height="44"
                prepend-icon="mdi-credit-card-outline"
                :loading="portalRedirecting"
                :disabled="portalRedirecting"
                @click="manageBilling"
              >
                Fix payment
              </VBtn>
              <VBtn
                v-if="recoveryPhase === 'waiting'"
                variant="text"
                color="primary"
                rounded="lg"
                height="44"
                @click="backToPlans"
              >
                Back to plans
              </VBtn>
              <VBtn
                v-if="recoveryPhase === 'timed-out'"
                variant="text"
                color="primary"
                rounded="lg"
                height="44"
                to="/fragrance"
              >
                Go to your collection
              </VBtn>
            </div>
          </div>
        </VFadeTransition>
      </div>

      <div v-else-if="loadError" key="load-error">
        <p class="text-h4 mb-2">Choose your plan</p>
        <v-alert type="error" variant="tonal" class="mb-4">
          We couldn't load billing plans. Please try again.
        </v-alert>
        <v-btn variant="flat" color="primary" rounded="lg" @click="loadAll">Try again</v-btn>
      </div>

      <div v-else-if="showSubscriptionView" key="subscription">
        <p id="subscription-title" tabindex="-1" class="text-h4 mb-2">Your subscription</p>
        <p class="text-body-2 text-medium-emphasis mb-8">Your plan, what it costs, and how to change it.</p>

        <div class="mb-8">
          <p class="text-h6">{{ subscription?.plan?.name ?? 'Subscription' }}</p>
          <p v-if="currentPlan" class="text-body-2 text-medium-emphasis">
            {{ formatPlanPrice(currentPlan) }} / {{ intervalPhrase(currentPlan) }}
          </p>
          <p class="text-body-2 text-medium-emphasis">{{ activeThroughLine }}</p>
        </div>

        <VExpandTransition>
          <div v-if="pending" role="region" aria-label="Scheduled plan change" class="mb-8">
            <VAlert
              type="info"
              variant="tonal"
              rounded="lg"
              density="comfortable"
              icon="mdi-calendar-clock-outline"
              role="presentation"
            >
              <p id="pending-change-title" tabindex="-1" class="text-subtitle-1">{{ pendingTitle }}</p>
              <p class="text-body-2 mb-3">{{ pendingBody }}</p>
              <VBtn
                variant="text"
                color="primary"
                rounded="lg"
                :loading="cancellingChange"
                :disabled="cancellingChange"
                class="d-block d-sm-inline-flex w-100 w-sm-auto"
                @click="keepCurrentPlan"
              >
                Keep my current plan
              </VBtn>
            </VAlert>
          </div>
        </VExpandTransition>

        <VAlert
          v-if="cancelChangeError"
          type="error"
          variant="tonal"
          rounded="lg"
          class="mb-4"
        >
          {{ cancelChangeError }}
        </VAlert>

        <VExpandTransition>
          <div v-if="showPlanSection" class="mb-8">
            <p class="text-h6 font-weight-regular mb-3">Plan</p>

            <v-skeleton-loader v-if="planChangeLoading && !planChange" type="list-item-two-line" />

            <div v-else-if="planChangeError">
              <VAlert type="error" variant="tonal" rounded="lg" class="mb-4">{{ planChangeError }}</VAlert>
              <VBtn variant="text" color="primary" rounded="lg" @click="fetchPlanChange">Try again</VBtn>
            </div>

            <p v-else-if="planChange?.cancel_at_period_end" class="text-body-2 text-medium-emphasis">
              Your subscription ends on {{ formatExpiryDate(planChange.current_period_end) }} and won't renew.
            </p>

            <template v-else>
              <div
                v-for="target in planChange?.available ?? []"
                :key="target.slug"
                class="d-flex flex-column flex-sm-row align-sm-center ga-4 mb-4"
              >
                <div class="flex-grow-1">
                  <p class="text-subtitle-1 mb-1">Switch to {{ target.name }} billing</p>
                  <p class="text-body-2 text-medium-emphasis">{{ switchBody(target) }}</p>
                </div>
                <VBtn
                  :id="`plan-switch-${target.slug}`"
                  variant="outlined"
                  color="primary"
                  rounded="lg"
                  prepend-icon="mdi-swap-horizontal"
                  height="44"
                  class="flex-shrink-0"
                  @click="openDialog(target)"
                >
                  Switch to {{ target.name }}
                </VBtn>
              </div>
            </template>
          </div>
        </VExpandTransition>

        <div v-if="showManageBilling" class="mb-8">
          <p class="text-h6 font-weight-regular mb-3">Payment and invoices</p>
          <VAlert
            v-if="portalError"
            type="error"
            variant="tonal"
            rounded="lg"
            class="mb-4"
          >
            {{ portalError }}
          </VAlert>
          <div class="d-flex flex-column flex-sm-row align-sm-center ga-4">
            <p class="text-body-2 text-medium-emphasis flex-grow-1">
              Update your card, see invoices and payment history, or cancel. This opens Stripe's secure billing page.
            </p>
            <VBtn
              variant="flat"
              color="primary"
              rounded="lg"
              prepend-icon="mdi-credit-card-outline"
              height="44"
              class="flex-shrink-0"
              :loading="portalRedirecting"
              :disabled="portalRedirecting"
              @click="manageBilling"
            >
              Manage billing
            </VBtn>
          </div>
          <p v-if="manageCaption" class="text-caption text-medium-emphasis mt-2">{{ manageCaption }}</p>
        </div>

        <ReceiptEmailSection v-if="showManageBilling" />

        <VBtn variant="text" color="primary" rounded="lg" to="/fragrance">Go to your collection</VBtn>
      </div>

      <div v-else-if="noActivePlans" key="no-plans" class="text-center py-16">
        <p class="text-body-2 text-medium-emphasis mb-6">
          Plans aren't available right now. Please check back soon.
        </p>
        <v-btn variant="flat" color="primary" rounded="lg" @click="fetchPlans">Try again</v-btn>
      </div>

      <div v-else key="populated">
        <p class="text-h4 mb-2">Choose your plan</p>
        <p class="text-body-2 text-medium-emphasis mb-8">
          One plan, billed however you'd rather pay. Either way, you get one recommendation run
          every 30 days.
        </p>

        <FixPaymentBlock
          v-if="showFixPayment && lapseReason === 'payment_failed'"
          prominent
          :loading="portalRedirecting"
          :error="portalError"
          @fix="manageBilling"
        />

        <ReceiptEmailSection v-if="showFixPayment" />

        <v-alert v-if="checkoutError" type="error" variant="tonal" rounded="lg" class="mb-4">
          {{ checkoutError }}
        </v-alert>

        <v-row class="ga-4">
          <v-col
            v-for="plan in plans"
            :key="plan.slug"
            cols="12"
            sm="6"
          >
            <v-card elevation="1" rounded="lg">
              <v-card-title class="text-h6 pt-6 px-6 pb-2 d-flex align-center ga-2">
                {{ plan.name }}
                <v-chip
                  v-if="showOneMonthFree(plan)"
                  size="small"
                  variant="tonal"
                  color="primary"
                >
                  One month free
                </v-chip>
              </v-card-title>
              <v-card-text class="px-6 pb-6">
                <p class="text-h4 font-weight-medium mb-1">
                  {{ formatPrice(plan) }}
                  <span class="text-body-2 text-medium-emphasis">/ {{ plan.billing_interval }}</span>
                </p>
                <p v-if="plan.billing_interval === 'year'" class="text-body-2 text-medium-emphasis mb-4">
                  {{ formatMonthlyEquivalent(plan) }} per month
                </p>
                <p class="text-body-2 text-medium-emphasis mb-1">
                  One recommendation run every 30 days, delivered by email.
                </p>
                <p class="text-body-2 text-medium-emphasis mb-4">
                  As a founding beta member, your feedback will help guide the product.
                </p>
                <v-btn
                  color="primary"
                  variant="flat"
                  rounded="lg"
                  block
                  :loading="checkoutSubmitting && selectedSlug === plan.slug"
                  :disabled="checkoutSubmitting"
                  @click="subscribe(plan.slug)"
                >
                  Subscribe
                </v-btn>
              </v-card-text>
            </v-card>
          </v-col>
        </v-row>

        <p class="text-caption text-medium-emphasis mt-6">
          Subscriptions renew automatically until cancelled, and payments are non-refundable.
          Payments are handled by Stripe. By subscribing you agree to the
          <NuxtLink to="/terms" target="_blank" rel="noopener" class="text-primary text-decoration-underline">Terms of Service<span class="d-sr-only"> (opens in a new tab)</span></NuxtLink>.
        </p>

        <p v-if="!showFixPayment" class="text-caption text-medium-emphasis mt-2">
          Receipts are sent to your account email. You can choose a different receipt address after you subscribe.
        </p>

        <FixPaymentBlock
          v-if="showFixPayment && lapseReason === null"
          :prominent="false"
          :loading="portalRedirecting"
          :error="portalError"
          @fix="manageBilling"
        />
      </div>
    </VFadeTransition>

    <PlanChangeDialog
      v-if="dialogTarget"
      v-model="dialogOpen"
      :target="dialogTarget"
      :current-name="subscription?.plan?.name ?? 'current'"
      :date="planChange?.current_period_end ?? null"
      :scheduling="scheduling"
      :error="dialogError"
      @confirm="confirmSwitch"
      @dismiss="dismissDialog"
    />

    <v-snackbar v-model="snackbar.show" timeout="6000" location="bottom" rounded="lg">
      {{ snackbar.message }}
    </v-snackbar>

    <v-overlay
      :model-value="portalRedirecting"
      persistent
      class="align-center justify-center"
      @keydown.esc.prevent
    >
      <div class="text-center">
        <v-progress-circular indeterminate color="primary" size="48" width="3" class="mb-4" />
        <p class="text-body-1">Opening Stripe's billing page&hellip;</p>
        <p class="text-caption text-medium-emphasis">Don't close this tab.</p>
      </div>
    </v-overlay>

    <v-overlay :model-value="checkoutSubmitting" persistent class="align-center justify-center">
      <div class="text-center">
        <v-progress-circular indeterminate color="primary" size="48" width="3" class="mb-4" />
        <p class="text-body-1">Redirecting you to Stripe to finish subscribing&hellip;</p>
        <p class="text-caption text-medium-emphasis">Don't close this tab.</p>
      </div>
    </v-overlay>
  </div>
</template>

<script setup lang="ts">
import { isAccountLocked, isPaymentPending, accountLockedMessage, type Plan } from '~/composables/useBilling'
import { FIX_PAYMENT_STARTED_KEY, FIX_PAYMENT_SEEN_FAILED_KEY } from '~/composables/usePortal'
import { isAuthError } from '~/composables/useApi'

definePageMeta({ layout: 'default' })

const {
  plans,
  plansLoading,
  plansError,
  fetchPlans,
  startCheckout,
  planChange,
  planChangeLoading,
  planChangeError,
  planChangeLocked,
  fetchPlanChange,
  schedulePlanChange,
  cancelPlanChange,
} = useBilling()
const {
  subscription,
  loading: subscriptionLoading,
  isExempt: exempt,
  canManageBilling,
  isLapsed,
  lapsedKind,
  fetchSubscription,
  refreshSubscription,
} = useSubscription()

const checkoutSubmitting = ref(false)
const selectedSlug = ref<string | null>(null)
const checkoutError = ref<string | null>(null)
const accountLocked = ref(false)
// The server's account_locked detail, shown in the locked view when a 409 put us there.
const lockedDetail = ref<string | null>(null)
const route = useRoute()
const router = useRouter()
const { portalRedirecting, portalError, portalLocked, manageBilling } = usePortal()
const supportEmail = useSupportEmail()
const lockedSentence = lockedRestartSentence(supportEmail)

const locked = computed(() => lapsedKind.value === 'revoked' || accountLocked.value)
const lapseReason = computed(() => subscription.value?.lapse_reason ?? null)
const showFixPayment = computed(
  () => isLapsed.value && lapsedKind.value === 'lapsed' && canManageBilling.value && !exempt.value,
)

const isLoading = computed(() => plansLoading.value || subscriptionLoading.value)
const loadError = computed(
  () => Boolean(plansError.value) && !plansLoading.value && !exempt.value && !showSubscriptionView.value && !locked.value
)

// The server's access verdict is the single source of truth; a lapsed subscriber keeps status 'active'.
const alreadyActive = computed(() => subscription.value?.has_active_access === true && !exempt.value)

const noActivePlans = computed(
  () => !isLoading.value && !exempt.value && !locked.value && !loadError.value && !alreadyActive.value && plans.value.length === 0
)

function formatPrice(plan: Plan): string {
  return formatPlanPrice(plan)
}

// Show the chip only when the annual price really is one monthly price cheaper than twelve months.
function showOneMonthFree(plan: Plan): boolean {
  if (plan.billing_interval !== 'year') return false
  const monthly = plans.value.find((p) => p.billing_interval === 'month' && p.currency === plan.currency)
  if (!monthly) return false
  return monthly.amount_cents * 12 - plan.amount_cents === monthly.amount_cents
}

function formatMonthlyEquivalent(plan: Plan): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: plan.currency }).format(
    plan.amount_cents / 12 / 100
  )
}

async function loadAll(): Promise<void> {
  await Promise.all([fetchPlans(), fetchSubscription()])
}

const showSubscriptionView = computed(() => alreadyActive.value && !exempt.value && !locked.value)
const showManageBilling = computed(() => showSubscriptionView.value && canManageBilling.value)
// Plan-change data (and its controls) exist only where the server says billing is manageable.
const canChangePlan = computed(() => showSubscriptionView.value && canManageBilling.value)
const pending = computed(() => (canChangePlan.value ? planChange.value?.pending ?? null : null))

const currentPlan = computed<Plan | null>(() => {
  const slug = subscription.value?.plan?.slug
  return slug ? plans.value.find((p) => p.slug === slug) ?? null : null
})

const activeThroughLine = computed(() => {
  const expires = subscription.value?.access_expires_at
  return expires ? `Active through ${formatExpiryDate(expires)}` : 'Active'
})

const showPlanSection = computed(() => {
  if (!canChangePlan.value) return false
  if (planChangeLoading.value && !planChange.value) return true
  if (planChangeError.value) return true
  const state = planChange.value
  if (!state) return false
  if (state.cancel_at_period_end) return true
  return !state.pending && state.available.length > 0
})

const manageCaption = computed(() => {
  if (pending.value) {
    return 'To cancel, first choose \u201cKeep my current plan\u201d above. Cancelling isn\u2019t available while a plan change is scheduled.'
  }
  if (planChange.value?.cancel_at_period_end) return null
  const expires = subscription.value?.access_expires_at
  return expires
    ? `If you cancel, you keep access through ${formatExpiryDate(expires)}. Nothing is refunded or charged partway.`
    : "If you cancel, you keep access through the period you've paid for. Nothing is refunded or charged partway."
})

function switchBody(target: Plan): string {
  const price = formatPlanPrice(target)
  return target.billing_interval === 'year'
    ? `Pay ${price} once a year instead of every month. The change happens when your current period ends; nothing is charged today.`
    : `Pay ${price} a month instead. Your annual term stays in place until it ends; nothing is charged today.`
}

const pendingPlan = computed<Plan | null>(() => {
  const slug = pending.value?.slug
  if (!slug) return null
  return (
    planChange.value?.available.find((p) => p.slug === slug) ?? plans.value.find((p) => p.slug === slug) ?? null
  )
})

const pendingTitle = computed(() => {
  const p = pending.value
  if (!p) return ''
  const date = formatExpiryDate(p.effective_at)
  return p.name ? `Switching to ${p.name} on ${date}` : `Your plan changes on ${date}`
})

const pendingBody = computed(() => {
  const p = pending.value
  if (!p) return ''
  const current = subscription.value?.plan?.name ?? 'current'
  const date = formatExpiryDate(p.effective_at)
  const target = pendingPlan.value
  if (!target) return `Until then you stay on your ${current} plan. Nothing is charged today.`
  const price = formatPlanPrice(target)
  return target.billing_interval === 'year'
    ? `Until then you stay on your ${current} plan. On ${date} you'll be charged ${price} for a year. Nothing is charged today.`
    : `Your ${current} term runs until ${date}. Then you'll be charged ${price} a month. Nothing is charged today.`
})

const snackbar = reactive({ show: false, message: '' })
function notify(message: string): void {
  snackbar.message = message
  snackbar.show = true
}

function focusById(id: string): void {
  nextTick(() => document.getElementById(id)?.focus())
}

// Any billing call answering account_locked (disputed account) lands here: show the server's
// own wording in the locked view, drop plan-change data, and refetch so the layout agrees.
// Never offers to re-subscribe on this account.
function enterLocked(detail: string): void {
  dialogOpen.value = false
  accountLocked.value = true
  lockedDetail.value = detail
  planChange.value = null
  void refreshSubscription()
  focusById('locked-title')
}

// Plan change dialog
const dialogOpen = ref(false)
const dialogTarget = ref<Plan | null>(null)
const dialogError = ref<string | null>(null)
const scheduling = ref(false)
const cancellingChange = ref(false)
const cancelChangeError = ref<string | null>(null)

function openDialog(target: Plan): void {
  dialogTarget.value = target
  dialogError.value = null
  dialogOpen.value = true
}

function dismissDialog(): void {
  if (scheduling.value) return
  dialogOpen.value = false
  if (dialogTarget.value) focusById(`plan-switch-${dialogTarget.value.slug}`)
}

async function confirmSwitch(): Promise<void> {
  const target = dialogTarget.value
  if (!target || scheduling.value) return
  const periodEnd = planChange.value?.current_period_end ?? null
  dialogError.value = null
  scheduling.value = true
  try {
    await schedulePlanChange(target.slug)
    dialogOpen.value = false
    await Promise.all([fetchPlanChange(), refreshSubscription()])
    if (planChange.value?.pending) {
      notify(
        periodEnd
          ? `Plan change scheduled. You'll switch to ${target.name} on ${formatExpiryDate(periodEnd)}.`
          : `Plan change scheduled. You'll switch to ${target.name} when your current period ends.`,
      )
      focusById('pending-change-title')
    } else {
      notify(
        subscription.value?.plan?.slug === target.slug
          ? `You're now on ${target.name} billing.`
          : 'Your billing details are up to date.',
      )
      focusById('subscription-title')
    }
  } catch (err: unknown) {
    if (isAuthError(err)) return
    if (isAccountLocked(err)) {
      enterLocked(accountLockedMessage(err, supportEmail))
      return
    }
    const e = err as { status?: number; data?: { code?: string } }
    const code = e?.data?.code
    if (e?.status === 409) {
      dialogOpen.value = false
      if (code === 'already_scheduled') {
        await fetchPlanChange()
        notify('A plan change was already scheduled. Here are the details.')
      } else if (code === 'same_plan') {
        await fetchPlanChange()
        notify("You're already on that plan.")
      } else if (code === 'cancellation_scheduled') {
        await fetchPlanChange()
      } else {
        await refreshSubscription()
      }
    } else if (e?.status === 429) {
      dialogError.value = 'Too many attempts. Please wait a moment and try again.'
    } else {
      dialogError.value = "We couldn't schedule the change just now. Please try again in a moment."
      void fetchPlanChange()
    }
  } finally {
    scheduling.value = false
  }
}

async function keepCurrentPlan(): Promise<void> {
  if (cancellingChange.value) return
  cancelChangeError.value = null
  cancellingChange.value = true
  const currentName = subscription.value?.plan?.name ?? 'your current plan'
  try {
    await cancelPlanChange()
    await fetchPlanChange()
    notify(`Plan change cancelled. You're staying on ${currentName}.`)
    const first = planChange.value?.available[0]
    if (first) focusById(`plan-switch-${first.slug}`)
  } catch (err: unknown) {
    if (isAuthError(err)) return
    if (isAccountLocked(err)) {
      enterLocked(accountLockedMessage(err, supportEmail))
      return
    }
    const e = err as { status?: number; data?: { code?: string } }
    if (e?.status === 404 && e.data?.code === 'no_pending_change') {
      await fetchPlanChange()
    } else if (e?.status === 409 && e.data?.code === 'exempt') {
      await refreshSubscription()
    } else if (e?.status === 429) {
      cancelChangeError.value = 'Too many attempts. Please wait a moment and try again.'
    } else {
      cancelChangeError.value = "We couldn't cancel the plan change just now. Please try again in a moment."
      void fetchPlanChange()
    }
  } finally {
    cancellingChange.value = false
  }
}

// Payment recovery: the return trip from Stripe's portal after "Fix payment".
type RecoveryPhase = 'checking' | 'retrying' | 'waiting' | 'restored' | 'declined' | 'timed-out'

const POLL_INTERVAL_MS = 5000
const MAX_POLLS = 18
const STILL_WAITING_THRESHOLD = 4
const STARTED_AT_TOLERANCE_MS = 60_000
const MARKER_MAX_AGE_MS = 60 * 60 * 1000

const recoveryActive = ref(false)
const recoveryPhase = ref<RecoveryPhase>('checking')
const recoveryTerminal = computed(
  () => recoveryPhase.value === 'restored' || recoveryPhase.value === 'declined' || recoveryPhase.value === 'timed-out',
)
const recovering = computed(() => recoveryActive.value && !locked.value && !exempt.value)
let pollCount = 0
// Newest failed attempt the user has already seen; only a later attempt may settle as declined again.
let seenFailedAttemptAt: number | null = null
let pollTimer: ReturnType<typeof setTimeout> | null = null
let cancelled = false

const recoveryCopy = computed(() => {
  switch (recoveryPhase.value) {
    case 'retrying':
      return {
        title: 'Retrying your payment',
        body: "Your card was added, and we're retrying the payment now. This page updates on its own, so there's no need to refresh or pay again.",
      }
    case 'waiting':
      return {
        title: 'Still checking',
        body: "We haven't seen a new payment method yet. If you just added one, it can take a minute to reach us, and this page keeps checking on its own. If you didn't add one, you can go back and try again.",
      }
    case 'restored':
      return {
        title: 'Access restored',
        body: 'Your payment went through and your subscription is active again. Thanks for sorting that out.',
      }
    case 'declined':
      return {
        title: "That payment didn't go through",
        body: 'This can happen when a card is declined or needs extra approval from your bank. Try another card and we\'ll retry the payment right away.',
      }
    case 'timed-out':
      return {
        title: "We couldn't confirm your payment yet",
        body: `If you added a card, we'll still retry the payment, and your access switches back on once it succeeds. Check back in a few minutes. If nothing has changed, write to ${supportEmail}.`,
      }
    default:
      return {
        title: 'Checking on your payment',
        body: "We're looking for the card you just added. This usually takes a few seconds.",
      }
  }
})

function readMarker(): number | null {
  try {
    const raw = sessionStorage.getItem(FIX_PAYMENT_STARTED_KEY)
    const n = raw === null ? NaN : Number(raw)
    return Number.isFinite(n) ? n : null
  } catch {
    return null
  }
}

function readSeenFailed(): number | null {
  try {
    const n = Number(sessionStorage.getItem(FIX_PAYMENT_SEEN_FAILED_KEY))
    return Number.isFinite(n) && n > 0 ? n : null
  } catch {
    return null
  }
}

function rememberSeenFailed(at: number): void {
  seenFailedAttemptAt = at
  try {
    sessionStorage.setItem(FIX_PAYMENT_SEEN_FAILED_KEY, String(at))
  } catch {
    // ignore
  }
}

function clearSeenFailed(): void {
  try {
    sessionStorage.removeItem(FIX_PAYMENT_SEEN_FAILED_KEY)
  } catch {
    // ignore
  }
}

function clearMarker(): void {
  try {
    sessionStorage.removeItem(FIX_PAYMENT_STARTED_KEY)
  } catch {
    // ignore
  }
}

function clearPollTimer(): void {
  if (pollTimer !== null) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function stripPortalQuery(): void {
  if (route.query.from === undefined) return
  const { from: _from, ...rest } = route.query
  void router.replace({ query: rest })
}

function settleRecovery(phase: RecoveryPhase): void {
  recoveryPhase.value = phase
  clearMarker()
  stripPortalQuery()
  focusById('recovery-title')
}

function evaluateRecovery(): boolean {
  const sub = subscription.value
  if (sub?.has_active_access) {
    clearSeenFailed()
    settleRecovery('restored')
    return true
  }
  const pr = sub?.payment_recovery ?? null
  if (pr?.state === 'failed') {
    const started = readMarker()
    const attemptedAt = new Date(pr.attempted_at).getTime()
    const isNewAttempt = seenFailedAttemptAt === null || attemptedAt > seenFailedAttemptAt
    if (isNewAttempt && (started === null || attemptedAt >= started - STARTED_AT_TOLERANCE_MS)) {
      rememberSeenFailed(attemptedAt)
      settleRecovery('declined')
      return true
    }
  }
  if (pollCount >= MAX_POLLS) {
    settleRecovery('timed-out')
    return true
  }
  if (pr?.state === 'retrying') recoveryPhase.value = 'retrying'
  else recoveryPhase.value = pollCount < STILL_WAITING_THRESHOLD ? 'checking' : 'waiting'
  return false
}

async function pollRecovery(): Promise<void> {
  pollCount++
  await refreshSubscription()
  if (cancelled || !recovering.value) return
  if (evaluateRecovery()) return
  pollTimer = setTimeout(pollRecovery, POLL_INTERVAL_MS)
}

function startRecovery(): void {
  clearPollTimer()
  pollCount = 0
  recoveryPhase.value = 'checking'
  recoveryActive.value = true
  void pollRecovery()
}

function checkAgain(): void {
  const pr = subscription.value?.payment_recovery
  if (pr?.state === 'failed') {
    const seen = new Date(pr.attempted_at).getTime()
    if (Number.isFinite(seen) && (seenFailedAttemptAt === null || seen > seenFailedAttemptAt)) rememberSeenFailed(seen)
  }
  startRecovery()
}

function backToPlans(): void {
  clearPollTimer()
  recoveryActive.value = false
  clearMarker()
  stripPortalQuery()
}

function portalReturnMarked(): boolean {
  if (route.query.from === 'portal') return true
  const started = readMarker()
  return started !== null && Date.now() - started < MARKER_MAX_AGE_MS
}

watch(locked, (isLocked) => {
  if (isLocked) {
    clearPollTimer()
    clearPendingTimer()
  }
})

// Only fetch plan-change data once the subscription resolves as active, non-exempt and manageable.
watch(canChangePlan, (visible) => {
  if (visible) void fetchPlanChange()
  else planChange.value = null
})

// A plan-change fetch or the portal answered account_locked.
watch(planChangeLocked, (detail) => {
  if (detail) enterLocked(detail)
})
watch(portalLocked, (isLocked) => {
  if (isLocked && portalError.value) enterLocked(portalError.value)
})

// Back from Stripe restores this page from bfcache with the overlay still on.
function onPageShow(event: PageTransitionEvent): void {
  if (!event.persisted) return
  portalRedirecting.value = false
  checkoutSubmitting.value = false
  selectedSlug.value = null
  void refreshSubscription()
  if (canChangePlan.value) void fetchPlanChange()
}

onMounted(async () => {
  window.addEventListener('pageshow', onPageShow)
  if (canChangePlan.value) void fetchPlanChange()
  const returning = portalReturnMarked()
  seenFailedAttemptAt = readSeenFailed()
  await loadAll()
  if (!returning) return
  if (locked.value || exempt.value || (showSubscriptionView.value && !subscription.value?.payment_recovery)) {
    // An active customer who only changed a card sees the normal view.
    clearMarker()
    stripPortalQuery()
    return
  }
  startRecovery()
})

onBeforeUnmount(() => {
  cancelled = true
  clearPollTimer()
  clearPendingTimer()
  window.removeEventListener('pageshow', onPageShow)
})

// 409 payment_pending: a payment is still being confirmed. Poll quietly (no skeleton flash) for
// about two minutes until access arrives, then the page moves to the active view on its own.
const PENDING_POLL_MS = 5000
const PENDING_MAX_POLLS = 24
let pendingTimer: ReturnType<typeof setTimeout> | null = null
let pendingPolls = 0
// Bumped whenever the poll is cancelled or restarted, so an in-flight pollPending that resumes
// after its await can tell it is stale and must not reschedule.
let pendingGeneration = 0

function clearPendingTimer(): void {
  pendingGeneration++
  if (pendingTimer !== null) {
    clearTimeout(pendingTimer)
    pendingTimer = null
  }
}

async function pollPending(): Promise<void> {
  const generation = pendingGeneration
  pendingPolls++
  await refreshSubscription(true)
  if (cancelled || locked.value || generation !== pendingGeneration) return
  if (subscription.value?.has_active_access) {
    checkoutError.value = null
    focusById('subscription-title')
    return
  }
  if (pendingPolls >= PENDING_MAX_POLLS) return
  pendingTimer = setTimeout(pollPending, PENDING_POLL_MS)
}

function startPendingPoll(): void {
  clearPendingTimer()
  pendingPolls = 0
  pendingTimer = setTimeout(pollPending, PENDING_POLL_MS)
}

async function subscribe(slug: string): Promise<void> {
  if (checkoutSubmitting.value) return
  clearPendingTimer()
  checkoutError.value = null
  checkoutSubmitting.value = true
  selectedSlug.value = slug
  try {
    const { url } = await startCheckout(slug)
    if (!isValidCheckoutUrl(url)) throw new Error('Unexpected checkout URL')
    window.location.href = url
    // Overlay stays mounted through the redirect; there is no success path that
    // hides it, because success means the page is about to unload.
  } catch (err: any) {
    checkoutSubmitting.value = false
    selectedSlug.value = null
    if (isAccountLocked(err)) {
      enterLocked(accountLockedMessage(err, supportEmail))
      return
    }
    if (isPaymentPending(err)) {
      const detail = err?.data?.detail
      checkoutError.value =
        typeof detail === 'string' && detail.trim()
          ? detail
          : 'Your payment is still being confirmed. Please check back in a minute; you will not be charged twice.'
      startPendingPoll()
      return
    }
    if (err?.status === 409 && err?.data?.code === 'subscription_still_active') {
      // The server's own explanation, in the checkout alert: no generic notify, no payment poll.
      const detail = err?.data?.detail
      checkoutError.value =
        typeof detail === 'string' && detail.trim()
          ? detail
          : 'You already have an active subscription.'
      return
    }
    if (err?.status === 409) {
      await refreshSubscription()
      notify('Your subscription status has changed. Here are the latest details.')
      return
    }
    if (err?.status === 429) {
      checkoutError.value = 'Too many attempts. Please wait a moment and try again.'
    } else if (typeof err?.status === 'number' && err.status >= 500) {
      checkoutError.value = "We couldn't start checkout just now. Please try again shortly."
    } else {
      checkoutError.value = 'Something went wrong starting checkout. Please try again.'
    }
  }
}
</script>
