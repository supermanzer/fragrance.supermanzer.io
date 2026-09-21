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
          <VBtn
            variant="flat"
            color="primary"
            rounded="lg"
            to="/billing"
          >
            {{ buttonLabel }}
          </VBtn>
        </div>
      </VAlert>
    </div>
  </VExpandTransition>
</template>

<script setup lang="ts">
const route = useRoute()
const { subscription, isLapsed, lapsedKind } = useSubscription()

const visible = computed(() => isLapsed.value && !route.path.startsWith('/billing'))

const message = computed(() => {
  switch (lapsedKind.value) {
    case 'revoked':
      return 'Access was paused when a payment dispute was opened on this account. Your collection and past recommendations are still here to read and export. Subscribe again to restore full access.'
    case 'lapsed': {
      const expires = subscription.value?.access_expires_at
      const ended = expires ? `Your subscription ended on ${formatExpiryDate(expires)}.` : 'Your subscription has ended.'
      return `${ended} Your collection and past recommendations are still here to read and export. Renew to make changes and receive new recommendations.`
    }
    default:
      return 'Your account is read-only until you subscribe. You can browse and export your collection now; adding fragrances and receiving recommendations needs a plan.'
  }
})

const buttonLabel = computed(() => (lapsedKind.value === 'lapsed' ? 'Renew' : 'View plans'))
</script>
