<template>
  <VExpandTransition>
    <div v-if="visible" role="region" aria-label="Account status">
      <VAlert
        type="info"
        variant="tonal"
        rounded="lg"
        density="comfortable"
        icon="mdi-book-open-variant-outline"
        role="presentation"
      >
        <div class="d-flex flex-column flex-sm-row align-sm-center ga-3">
          <p class="text-body-2 flex-grow-1 mb-0" style="max-width: 720px">{{ message }}</p>
          <div class="d-flex flex-column flex-sm-row align-start ga-2">
            <VBtn
              variant="flat"
              color="primary"
              rounded="lg"
              to="/billing"
            >
              {{ buttonLabel }}
            </VBtn>
            <VBtn
              v-if="lapsedKind === 'lapsed' && canManageBilling"
              variant="text"
              color="primary"
              rounded="lg"
              :loading="portalRedirecting"
              :disabled="portalRedirecting"
              @click="manageBilling"
            >
              Fix payment
            </VBtn>
          </div>
        </div>
        <p v-if="portalError" class="text-caption mt-2 mb-0">{{ portalError }}</p>
      </VAlert>
    </div>
  </VExpandTransition>
</template>

<script setup lang="ts">
const route = useRoute()
const { subscription, isLapsed, lapsedKind, canManageBilling } = useSubscription()
const { portalRedirecting, portalError, manageBilling } = usePortal()

const visible = computed(() => isLapsed.value && !route.path.startsWith('/billing'))

const message = computed(() => {
  switch (lapsedKind.value) {
    case 'revoked':
      return 'Access was paused when a payment dispute was opened on this account. Your collection and past recommendations are still here to read and export. Write to support@supermanzer.io to sort it out.'
    case 'lapsed': {
      const expires = subscription.value?.access_expires_at
      const ended = expires ? `Your subscription ended on ${formatExpiryDate(expires)}.` : 'Your subscription has ended.'
      return `${ended} Your collection and past recommendations are still here to read and export. Renew to make changes and receive new recommendations.`
    }
    default:
      return 'Your account is read-only until you subscribe. You can browse and export your collection now; adding fragrances and receiving recommendations needs a plan.'
  }
})

const buttonLabel = computed(() => (
  lapsedKind.value === 'lapsed' ? 'Renew' : lapsedKind.value === 'revoked' ? 'See details' : 'View plans'
))
</script>
