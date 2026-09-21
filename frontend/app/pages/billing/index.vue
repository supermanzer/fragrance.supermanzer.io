<template>
  <div style="max-width: 720px; margin: 0 auto">
    <VFadeTransition mode="out-in">
      <div v-if="isLoading" key="loading">
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

      <div v-else-if="loadError" key="load-error">
        <p class="text-h4 mb-2">Choose your plan</p>
        <v-alert type="error" variant="tonal" class="mb-4">
          We couldn't load billing plans. Please try again.
        </v-alert>
        <v-btn variant="flat" color="primary" rounded="lg" @click="loadAll">Try again</v-btn>
      </div>

      <div v-else-if="alreadyActive" key="already-active" class="text-center py-16">
        <v-icon icon="mdi-check-decagram-outline" size="48" color="primary" class="mb-4" />
        <p class="text-h6 font-weight-regular mb-2">You're already subscribed</p>
        <p class="text-body-2 text-medium-emphasis mb-6">{{ alreadyActiveBody }}</p>
        <v-btn color="primary" variant="flat" rounded="lg" to="/fragrance">Go to your collection</v-btn>
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

        <v-alert v-if="checkoutError" type="error" variant="tonal" class="mb-4">
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
                  v-if="plan.billing_interval === 'year'"
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
                <p class="text-body-2 text-medium-emphasis mb-4">
                  One recommendation run every 30 days, delivered by email.
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
      </div>
    </VFadeTransition>

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
import type { Plan } from '~/composables/useBilling'

definePageMeta({ layout: 'default' })

const { plans, plansLoading, plansError, fetchPlans, startCheckout } = useBilling()
const { subscription, loading: subscriptionLoading, isExempt: exempt, fetchSubscription, refreshSubscription } = useSubscription()

const checkoutSubmitting = ref(false)
const selectedSlug = ref<string | null>(null)
const checkoutError = ref<string | null>(null)

const isLoading = computed(() => plansLoading.value || subscriptionLoading.value)
const loadError = computed(() => Boolean(plansError.value) && !plansLoading.value && !exempt.value)

const alreadyActive = computed(() => {
  const sub = subscription.value
  if (!sub || sub.status !== 'active') return false
  if (sub.access_expires_at === null) return true
  return new Date(sub.access_expires_at) > new Date()
})

const alreadyActiveBody = computed(() => {
  const sub = subscription.value
  const name = sub?.plan?.name ?? 'subscription'
  if (sub?.access_expires_at) {
    return `Your ${name} plan is active through ${formatExpiry(sub.access_expires_at)}. No need to do anything else.`
  }
  return `Your ${name} plan is active. No need to do anything else.`
})

const noActivePlans = computed(
  () => !isLoading.value && !exempt.value && !loadError.value && !alreadyActive.value && plans.value.length === 0
)

function formatPrice(plan: Plan): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: plan.currency }).format(
    plan.amount_cents / 100
  )
}

function formatMonthlyEquivalent(plan: Plan): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: plan.currency }).format(
    plan.amount_cents / 12 / 100
  )
}

function formatExpiry(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
}

async function loadAll(): Promise<void> {
  await Promise.all([fetchPlans(), fetchSubscription()])
}

onMounted(loadAll)

async function subscribe(slug: string): Promise<void> {
  checkoutError.value = null
  checkoutSubmitting.value = true
  selectedSlug.value = slug
  try {
    const { url } = await startCheckout(slug)
    window.location.href = url
    // Overlay stays mounted through the redirect; there is no success path that
    // hides it, because success means the page is about to unload.
  } catch (err: any) {
    checkoutSubmitting.value = false
    if (err?.status === 409) {
      selectedSlug.value = null
      await refreshSubscription()
      return
    }
    selectedSlug.value = null
    if (err?.status === 429) {
      checkoutError.value = 'Too many attempts. Please wait a moment and try again.'
    } else {
      checkoutError.value = 'Something went wrong starting checkout. Please try again.'
    }
  }
}
</script>
