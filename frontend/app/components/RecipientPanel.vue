<template>
  <v-card
    v-if="visible"
    :elevation="isDelivery ? 1 : 0"
    :rounded="isDelivery ? 'lg' : 0"
    :color="isDelivery ? undefined : 'transparent'"
    :max-width="isDelivery ? 560 : undefined"
    :class="isDelivery ? '' : 'mb-8'"
  >
    <v-card-title
      role="heading"
      aria-level="2"
      :class="isDelivery ? 'pt-6 px-6 pb-2' : 'pa-0 text-h6 font-weight-regular mb-3'"
    >
      {{ copy.title }}
    </v-card-title>

    <v-card-text :class="isDelivery ? 'px-6 pb-6' : 'pa-0'">
      <v-skeleton-loader
        v-if="!recipient.loaded.value && !recipient.loadFailed.value"
        type="heading, text, text, button"
        max-width="560"
      />

      <div v-else-if="recipient.loadFailed.value || recipient.unknown.value">
        <v-alert type="error" variant="tonal" rounded="lg" class="mb-2">
          {{ copy.loadFailed }}
        </v-alert>
        <v-btn
          variant="text"
          color="primary"
          rounded="lg"
          :loading="recipient.loading.value"
          @click="retryLoad"
        >
          Try again
        </v-btn>
      </div>

      <div v-else>
        <v-alert
          v-if="recipient.subscriptionRequired.value"
          type="info"
          variant="tonal"
          rounded="lg"
          density="comfortable"
          class="mb-4"
        >
          {{ READ_ONLY_WRITE_NOTICE }}
        </v-alert>
        <AppAlert v-model="actionErrorModel" class="mb-4" />

        <div :class="isDelivery ? 'mb-4' : 'mb-2'">
          <p class="text-caption text-medium-emphasis">{{ copy.caption }}</p>
          <p class="text-body-1 text-break">{{ recipient.verified.value }}</p>
        </div>
        <p
          v-if="copy.explainer"
          :id="`${purpose}-explainer`"
          class="text-body-2 text-medium-emphasis mb-2"
        >
          {{ copy.explainer }}
        </p>

        <VExpandTransition>
          <div v-if="state === 'pending'" class="mt-4">
            <v-alert
              type="info"
              variant="tonal"
              rounded="lg"
              density="comfortable"
              role="presentation"
            >
              <h3 ref="alertTitle" tabindex="-1" class="text-body-1">Check your inbox</h3>
              <p class="text-body-2 mb-1 text-break">{{ copy.pendingBody(recipient.pending.value, recipient.verified.value) }}</p>
              <p class="text-caption text-medium-emphasis mb-3">
                Nothing there after a few minutes? Check your spam folder, or send it again.
              </p>
              <div class="d-flex flex-wrap ga-2">
                <LapsedGate :locked="gated" label="Resend link" :reason="copy.gateReason">
                  <v-btn
                    ref="resendBtn"
                    variant="text"
                    color="primary"
                    rounded="lg"
                    :class="{ 'v-btn--disabled': recipient.cooling.value }"
                    :loading="recipient.resending.value"
                    :disabled="gated"
                    :aria-disabled="recipient.cooling.value ? 'true' : undefined"
                    :aria-describedby="recipient.cooling.value ? cooldownId : undefined"
                    @click="onResend"
                  >
                    Resend link
                  </v-btn>
                </LapsedGate>
                <v-btn
                  variant="text"
                  color="secondary"
                  rounded="lg"
                  :loading="recipient.cancelling.value"
                  @click="onCancel"
                >
                  Cancel change
                </v-btn>
              </div>
              <p
                v-if="recipient.cooling.value"
                :id="cooldownId"
                class="text-caption text-medium-emphasis mt-2"
              >
                You can send another link in about a minute.
                <span aria-hidden="true">({{ recipient.cooldownSeconds.value }}s)</span>
              </p>
            </v-alert>
          </div>
          <div v-else-if="state === 'expired'" class="mt-4">
            <v-alert
              type="warning"
              variant="tonal"
              rounded="lg"
              density="comfortable"
              role="presentation"
            >
              <h3 ref="alertTitle" tabindex="-1" class="text-body-1">That link expired</h3>
              <p class="text-body-2 mb-3 text-break">{{ copy.expiredBody(recipient.pending.value, recipient.verified.value) }}</p>
              <div class="d-flex flex-wrap ga-2">
                <LapsedGate :locked="gated" label="Send a new link" :reason="copy.gateReason">
                  <v-btn
                    ref="resendBtn"
                    variant="text"
                    color="primary"
                    rounded="lg"
                    :class="{ 'v-btn--disabled': recipient.cooling.value }"
                    :loading="recipient.resending.value"
                    :disabled="gated"
                    :aria-disabled="recipient.cooling.value ? 'true' : undefined"
                    :aria-describedby="recipient.cooling.value ? cooldownId : undefined"
                    @click="onResend"
                  >
                    Send a new link
                  </v-btn>
                </LapsedGate>
                <v-btn
                  variant="text"
                  color="secondary"
                  rounded="lg"
                  :loading="recipient.cancelling.value"
                  @click="onCancel"
                >
                  Cancel change
                </v-btn>
              </div>
              <p
                v-if="recipient.cooling.value"
                :id="cooldownId"
                class="text-caption text-medium-emphasis mt-2"
              >
                You can send another link in about a minute.
                <span aria-hidden="true">({{ recipient.cooldownSeconds.value }}s)</span>
              </p>
            </v-alert>
          </div>
        </VExpandTransition>

        <VExpandTransition>
          <v-form v-if="formOpen" class="mt-4" @submit.prevent="onSubmit" @keydown.esc="closeForm">
            <v-text-field
              ref="emailField"
              v-model="email"
              :label="copy.fieldLabel"
              type="email"
              variant="outlined"
              rounded="lg"
              autocomplete="email"
              inputmode="email"
              hide-details="auto"
              persistent-hint
              :hint="state === 'none' ? copy.hintNone(recipient.verified.value) : copy.hintPending(recipient.verified.value)"
              :error-messages="recipient.emailError.value ?? undefined"
              class="mb-4"
            />
            <v-text-field
              ref="passwordField"
              v-model="password"
              label="Current password"
              type="password"
              variant="outlined"
              rounded="lg"
              autocomplete="current-password"
              hide-details="auto"
              persistent-hint
              :hint="copy.passwordHint"
              :error-messages="recipient.passwordError.value ?? undefined"
              class="mb-4"
            />
            <div class="d-flex flex-column flex-sm-row justify-sm-end ga-2">
              <v-btn variant="text" color="secondary" rounded="lg" @click="closeForm">Never mind</v-btn>
              <v-btn
                type="submit"
                variant="flat"
                color="primary"
                rounded="lg"
                :block="xs"
                :loading="recipient.submitting.value"
                :disabled="recipient.submitting.value"
              >
                Send confirmation link
              </v-btn>
            </div>
          </v-form>
        </VExpandTransition>

        <div v-if="!formOpen" class="mt-2">
          <LapsedGate :locked="gated" label="Change address" :reason="copy.gateReason">
            <v-btn
              ref="changeBtn"
              variant="text"
              color="primary"
              rounded="lg"
              :prepend-icon="isDelivery ? undefined : 'mdi-email-edit-outline'"
              :disabled="gated"
              :aria-describedby="copy.explainer ? `${purpose}-explainer` : undefined"
              @click="openForm"
            >
              {{ copy.changeButton }}
            </v-btn>
          </LapsedGate>
        </div>

        <p role="status" aria-live="polite" class="text-caption text-medium-emphasis mt-3">
          {{ recipient.feedback.value ?? ' ' }}
        </p>
      </div>
    </v-card-text>
  </v-card>
</template>

<script setup lang="ts">
import { useDisplay } from 'vuetify'
import { useRecipient, type RecipientPurpose } from '~/composables/useRecipient'
import type { FragranceConfig } from '~/composables/useSettings'

// The verified / pending / expired address UI shared by Settings ("delivery") and Billing
// ("receipt"). All state lives in useRecipient; this component is markup, focus and copy.
// Settings already reads GET /config/ for the schedule card, so it hands that result down
// (`snapshot`) once its own fetch has finished (`settled`). Omit `settled` and the panel
// fetches for itself, as Billing does.
const props = defineProps<{
  readonly purpose: RecipientPurpose
  readonly snapshot?: FragranceConfig | null
  readonly settled?: boolean
}>()

const isDelivery = props.purpose === 'delivery'
const recipient = useRecipient(props.purpose)
const { isLapsed, refreshSubscription } = useSubscription()
const { xs } = useDisplay()

// Receipt mail is how a lapsed user learns a card failed, so only the picks address is gated.
const gated = computed(() => isDelivery && isLapsed.value)
const cooldownId = `${props.purpose}-resend-cooldown`

interface PanelCopy {
  title: string
  caption: string
  explainer: string | null
  changeButton: string
  fieldLabel: string
  passwordHint: string
  gateReason: string
  loadFailed: string
  hintNone: (verified: string | null) => string
  hintPending: (verified: string | null) => string
  pendingBody: (pending: string | null, verified: string | null) => string
  expiredBody: (pending: string | null, verified: string | null) => string
}

const COPY: Record<RecipientPurpose, PanelCopy> = {
  delivery: {
    title: 'Delivery',
    caption: 'Picks and notices are sent to',
    explainer: null,
    changeButton: 'Change address',
    fieldLabel: 'New email address',
    passwordHint: 'We ask for this so only you can redirect your picks.',
    gateReason: 'Changing your delivery address needs an active subscription.',
    loadFailed: "We couldn't load your delivery address just now.",
    hintNone: (v) => `We'll send a confirmation link to this address. Picks keep going to ${v ?? 'your current address'} until you confirm.`,
    hintPending: (v) => `This replaces the pending address. Picks keep going to ${v ?? 'your current address'}.`,
    pendingBody: (p, v) => `We sent a confirmation link to ${p}. Until you confirm it, picks keep going to ${v}.`,
    expiredBody: (p, v) => `The confirmation link for ${p} expired. Picks are still going to ${v}.`,
  },
  receipt: {
    title: 'Receipt email',
    caption: 'Receipts and invoices are sent to',
    explainer:
      "This can be different from your sign-in email. Changing it doesn't affect your login or where your monthly picks go.",
    changeButton: 'Change receipt address',
    fieldLabel: 'New receipt email address',
    passwordHint: 'We ask for this so only you can redirect your receipts.',
    gateReason: 'Changing your receipt address needs an active subscription.',
    loadFailed: "We couldn't load your receipt address just now.",
    hintNone: (v) => `We'll send a confirmation link to this address. Receipts keep going to ${v ?? 'your current address'} until you confirm.`,
    hintPending: (v) => `This replaces the pending address. Receipts keep going to ${v ?? 'your current address'}.`,
    pendingBody: (p, v) => `We sent a confirmation link to ${p}. Until you confirm it, receipts keep going to ${v}.`,
    expiredBody: (p, v) => `The confirmation link for ${p} expired. Receipts are still going to ${v}.`,
  },
}

const copy = COPY[props.purpose]

const state = computed(() => recipient.pendingState.value)
// Receipt: nothing to show without a Stripe customer, or for exempt / locked accounts.
const visible = computed(() => isDelivery || (recipient.hasCustomer.value && recipient.blocked.value === null))

const actionErrorModel = computed<string | null>({
  get: () => recipient.actionError.value,
  set: (value) => {
    recipient.actionError.value = value
  },
})

const formOpen = ref(false)
const email = ref('')
const password = ref('')

interface Focusable {
  focus: () => void
}
const emailField = ref<Focusable | null>(null)
const passwordField = ref<Focusable | null>(null)
const changeBtn = ref<{ $el?: HTMLElement } | null>(null)
const resendBtn = ref<{ $el?: HTMLElement } | null>(null)
const alertTitle = ref<HTMLElement | null>(null)

function focusAfterRender(focus: () => void): void {
  nextTick(() => focus())
}

function openForm(): void {
  if (gated.value) return
  recipient.clearErrors()
  formOpen.value = true
  focusAfterRender(() => emailField.value?.focus())
}

function closeForm(): void {
  formOpen.value = false
  email.value = ''
  password.value = ''
  recipient.clearErrors()
  focusAfterRender(() => changeBtn.value?.$el?.focus())
}

async function onSubmit(): Promise<void> {
  const sent = await recipient.requestChange(email.value, password.value)
  // The password never survives a submit, success or failure.
  password.value = ''
  if (sent) {
    formOpen.value = false
    email.value = ''
    focusAfterRender(() => alertTitle.value?.focus())
  } else if (recipient.passwordError.value) {
    focusAfterRender(() => passwordField.value?.focus())
  } else if (recipient.emailError.value) {
    focusAfterRender(() => emailField.value?.focus())
  } else if (recipient.pendingState.value !== 'none') {
    // 503 send_unavailable: the change was stored, so the pending alert now offers Resend.
    formOpen.value = false
    focusAfterRender(() => alertTitle.value?.focus())
  }
}

async function onResend(): Promise<void> {
  // Cooling down is aria-disabled rather than disabled, so clicks must be ignored here.
  if (recipient.cooling.value || recipient.resending.value || gated.value) return
  await recipient.resend()
  focusAfterRender(() => resendBtn.value?.$el?.focus())
}

async function onCancel(): Promise<void> {
  const cancelled = await recipient.cancel()
  if (cancelled) focusAfterRender(() => changeBtn.value?.$el?.focus())
}

async function retryLoad(): Promise<void> {
  await recipient.load()
}

// A confirmation on another device should show up when the user comes back to this tab.
function onVisibility(): void {
  if (document.visibilityState !== 'visible') return
  if (state.value === 'none' || recipient.submitting.value || recipient.resending.value) return
  void recipient.load(true)
}

// A 409 exempt / account_locked from any action means the page's own status is stale.
watch(
  () => recipient.blocked.value,
  (blocked) => {
    if (blocked) void refreshSubscription()
  },
)

// Shared-read mode: adopt the parent's result once, or fall back to our own read if the
// parent's failed (which also drives the error state and the Try again button).
const sharedRead = props.settled !== undefined
let adopted = false
watch(
  () => props.settled,
  (settled) => {
    if (!sharedRead || !settled || adopted) return
    adopted = true
    if (props.snapshot) recipient.applySnapshot(props.snapshot)
    else void recipient.load()
  },
  { immediate: true },
)

onMounted(() => {
  if (!sharedRead) void recipient.load()
  document.addEventListener('visibilitychange', onVisibility)
})

onBeforeUnmount(() => {
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>
