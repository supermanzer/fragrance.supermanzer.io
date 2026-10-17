<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
    <div aria-live="polite">
      <VFadeTransition mode="out-in">
        <div v-if="pageState === 'ready'" key="ready">
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            {{ readyCopy.title }}
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-2">{{ readyCopy.body }}</p>
          <p class="text-caption text-medium-emphasis mb-6">Nothing changes until you confirm.</p>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            :loading="loading"
            :disabled="loading"
            @click="confirm"
          >
            Confirm this address
          </v-btn>
          <span v-if="loading" class="d-sr-only">Confirming your address.</span>
          <div class="text-center mt-4">
            <NuxtLink to="/" class="text-body-2 text-primary">Not now</NuxtLink>
          </div>
        </div>

        <div v-else-if="pageState === 'confirmed' || pageState === 'already'" key="done" class="text-center">
          <v-icon icon="mdi-check-circle-outline" size="48" color="success" class="mb-4" aria-hidden="true" />
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            {{ doneCopy.title }}
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-6 text-break">{{ doneCopy.body }}</p>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            :to="signedIn ? appDestination : signInTarget"
          >
            {{ signedIn ? doneCopy.signedInLabel : 'Sign in' }}
          </v-btn>
        </div>

        <div v-else-if="pageState === 'expired' || pageState === 'superseded'" key="dead" class="text-center">
          <v-icon icon="mdi-link-off" size="48" class="text-medium-emphasis mb-4" aria-hidden="true" />
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            {{ deadCopy.title }}
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-6">{{ deadCopy.body }}</p>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            :to="signedIn ? appDestination : signInTarget"
          >
            {{ signedIn ? deadCopy.signedInLabel : 'Sign in' }}
          </v-btn>
        </div>

        <div v-else-if="pageState === 'invalid'" key="invalid" class="text-center">
          <v-icon icon="mdi-link-off" size="48" class="text-medium-emphasis mb-4" aria-hidden="true" />
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            This link isn't valid
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-4">
            It may have been copied incompletely. Open the link from the email again, or request a new one from Settings.
          </p>
          <p class="text-body-2 text-medium-emphasis mb-6">
            If you still need help, write to
            <a :href="`mailto:${supportEmail}`" rel="noreferrer noopener" class="text-primary text-break">{{ supportEmail }}</a>.
          </p>
          <NuxtLink to="/" class="text-body-2 text-primary">Back to home</NuxtLink>
        </div>

        <div v-else-if="pageState === 'stripe'" key="stripe">
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-4">
            We couldn't update your receipt address
          </h1>
          <v-alert type="error" variant="tonal" class="mb-4">
            Your link is still valid. Please try again in a minute.
          </v-alert>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            :loading="loading"
            :disabled="loading"
            @click="confirm"
          >
            Try again
          </v-btn>
          <span v-if="loading" class="d-sr-only">Confirming your address.</span>
          <p class="text-body-2 text-medium-emphasis text-center mt-4">
            If this keeps happening, write to
            <a :href="`mailto:${supportEmail}`" rel="noreferrer noopener" class="text-primary text-break">{{ supportEmail }}</a>.
          </p>
          <div class="text-center mt-4">
            <NuxtLink to="/" class="text-body-2 text-primary">Not now</NuxtLink>
          </div>
        </div>

        <div v-else key="error">
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-4">
            Something went wrong
          </h1>
          <v-alert type="error" variant="tonal" class="mb-4">
            We couldn't confirm your address just now. Please try again in a minute.
          </v-alert>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            :loading="loading"
            :disabled="loading"
            @click="confirm"
          >
            Try again
          </v-btn>
          <span v-if="loading" class="d-sr-only">Confirming your address.</span>
          <div class="text-center mt-4">
            <NuxtLink to="/" class="text-body-2 text-primary">Not now</NuxtLink>
          </div>
        </div>
      </VFadeTransition>
    </div>
  </v-card>
</template>

<script setup lang="ts">
// Landing page for the link in a recipient-confirmation email (delivery or receipt address).
//
// This page must never touch the session: no clearTokens(), no useSessionReset(), no writes to
// authStatus, and no checkAuth() (which can clear tokens). The link is often opened on a phone
// while the session lives on a laptop; possession of the mailbox is the proof, and the token
// binds the change to the right user. Signed-in state is read once, only to pick the button.
definePageMeta({ layout: 'auth', auth: false })

type PageState =
  | 'ready'
  | 'confirmed'
  | 'already'
  | 'expired'
  | 'superseded'
  | 'invalid'
  | 'stripe'
  | 'error'
type Purpose = 'delivery' | 'receipt'

interface ConfirmResponse {
  status?: string
  purpose?: string
  recipient_email?: string
  receipt_email?: string
}

const config = useRuntimeConfig()
const supportEmail = useSupportEmail()
const route = useRoute()
const router = useRouter()

// Read once into memory (SSR and client both see it in the initial URL). onMounted then strips
// it from the address bar; this ref keeps it so "Try again" after a 502 still works.
const rawToken = route.query.token
const token = ref(typeof rawToken === 'string' ? rawToken.trim() : '')

// The query value only picks the pre-confirmation copy and is never trusted: every result
// below keys off the purpose the API returns.
const hintPurpose: Purpose = route.query.purpose === 'receipt' ? 'receipt' : 'delivery'

const pageState = ref<PageState>(token.value ? 'ready' : 'invalid')
const resultPurpose = ref<Purpose>(hintPurpose)
const resultAddress = ref('')
const loading = ref(false)
const signedIn = ref(false)
const heading = ref<HTMLElement | null>(null)

useSeoMeta({
  title: () => (hintPurpose === 'receipt' ? 'Confirm your receipt address' : 'Confirm your delivery address'),
  robots: 'noindex',
})

// The URL carries a single-use token: send no Referer from this page (the POST and any link).
useHead({ meta: [{ name: 'referrer', content: 'no-referrer' }] })

onMounted(() => {
  // Same presence test the global auth middleware uses. Read-only.
  signedIn.value = Boolean(localStorage.getItem('auth_access'))

  // Drop the token from the address bar and history entry. Same path, so the page is not
  // remounted and the in-memory token above survives.
  if (route.query.token !== undefined) {
    const { token: _omitted, ...rest } = route.query
    void router.replace({ path: route.path, query: rest, hash: route.hash })
  }
})

watch(pageState, async () => {
  await nextTick()
  // Wait for the out-in fade to mount the new block before focusing.
  setTimeout(() => heading.value?.focus(), 250)
})

function parsePurpose(value: unknown): Purpose | null {
  return value === 'delivery' || value === 'receipt' ? value : null
}

const isReceipt = computed(() => resultPurpose.value === 'receipt')
// Where a signed-in visitor goes, and where sign-in returns a signed-out one.
const appDestination = computed(() => (isReceipt.value ? '/billing' : '/settings'))
const signInTarget = computed(() => ({ path: '/auth/login', query: { redirect: appDestination.value } }))

const readyCopy = computed(() =>
  hintPurpose === 'receipt'
    ? {
        title: 'Confirm your receipt address',
        body: 'Confirm that Fragrance Journey may send payment receipts and invoices to the address this email was sent to.',
      }
    : {
        title: 'Confirm your delivery address',
        body: 'Confirm that Fragrance Journey may send monthly picks and notices to the address this email was sent to.',
      },
)

const doneCopy = computed(() => {
  const address = resultAddress.value
  if (isReceipt.value) {
    return pageState.value === 'confirmed'
      ? {
          title: 'Receipt address confirmed',
          body: `Receipts and invoices will now go to ${address}.`,
          signedInLabel: 'Back to billing',
        }
      : {
          title: 'This address is already confirmed',
          body: `${address} already receives your receipts and invoices. There's nothing more to do.`,
          signedInLabel: 'Back to billing',
        }
  }
  return pageState.value === 'confirmed'
    ? {
        title: 'Delivery address confirmed',
        body: `Your monthly picks and notices will now go to ${address}.`,
        signedInLabel: 'Back to settings',
      }
    : {
        title: 'This address is already confirmed',
        body: `${address} is already set up to receive your picks and notices. There's nothing more to do.`,
        signedInLabel: 'Back to settings',
      }
})

const deadCopy = computed(() => {
  const expired = pageState.value === 'expired'
  if (isReceipt.value) {
    return expired
      ? {
          title: 'This link has expired',
          body: 'Confirmation links work for 24 hours. Open Billing and choose "Send a new link" to get a fresh one.',
          signedInLabel: 'Open billing',
        }
      : {
          title: 'This link is no longer active',
          body: 'The receipt address was changed or cancelled after this email was sent. If you still want this address, request a new link from Billing.',
          signedInLabel: 'Open billing',
        }
  }
  return expired
    ? {
        title: 'This link has expired',
        body: 'Confirmation links work for 24 hours. Open Settings and choose "Send a new link" to get a fresh one.',
        signedInLabel: 'Open settings',
      }
    : {
        title: 'This link is no longer active',
        body: 'The delivery address was changed or cancelled after this email was sent. If you still want this address, request a new link from Settings.',
        signedInLabel: 'Open settings',
      }
})

async function confirm(): Promise<void> {
  if (loading.value) return
  if (!token.value) {
    pageState.value = 'invalid'
    return
  }
  loading.value = true
  try {
    const data = await $fetch<ConfirmResponse>('/auth/confirm-recipient/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      referrerPolicy: 'no-referrer',
      body: { token: token.value },
    })
    const purpose = parsePurpose(data?.purpose) ?? hintPurpose
    resultPurpose.value = purpose
    resultAddress.value = (purpose === 'receipt' ? data?.receipt_email : data?.recipient_email) ?? ''
    pageState.value = data?.status === 'already_confirmed' ? 'already' : 'confirmed'
  } catch (err: unknown) {
    const e = err as { status?: number; data?: { code?: string; purpose?: string } } | null
    const code = e?.data?.code
    const purpose = parsePurpose(e?.data?.purpose)
    if (purpose) resultPurpose.value = purpose
    if (e?.status === 400 && code === 'expired') pageState.value = 'expired'
    else if (e?.status === 400 && code === 'superseded') pageState.value = 'superseded'
    else if (e?.status === 400) pageState.value = 'invalid'
    else if (e?.status === 502 && code === 'stripe_unavailable') pageState.value = 'stripe'
    else pageState.value = 'error'
  } finally {
    loading.value = false
  }
}
</script>
