<template>
  <VExpandTransition>
    <div v-if="kind" role="region" aria-label="Run status" class="mb-6">
      <VAlert
        :type="kind === 'exhausted' ? 'error' : 'info'"
        :icon="kind === 'exhausted' ? 'mdi-alert-circle-outline' : 'mdi-timer-sand'"
        variant="tonal"
        rounded="lg"
        density="comfortable"
        role="presentation"
      >
        <div class="d-flex flex-column flex-sm-row align-sm-center ga-3">
          <div class="flex-grow-1">
            <p id="run-notice-title" tabindex="-1" class="text-subtitle-1 mb-1">{{ title }}</p>
            <p class="text-body-2 mb-0">{{ body }}</p>
          </div>
          <VBtn
            v-if="showSupport"
            variant="text"
            color="error"
            rounded="lg"
            prepend-icon="mdi-email-outline"
            class="flex-shrink-0 align-self-start align-self-sm-center mt-3 mt-sm-0"
            :href="`mailto:${supportEmail}?subject=This%20month's%20run`"
          >
            Email support
          </VBtn>
        </div>
      </VAlert>
    </div>
  </VExpandTransition>
</template>

<script setup lang="ts">
const supportEmail = useSupportEmail()

defineProps<{
  readonly kind: 'retrying' | 'running-again' | 'exhausted' | 'stopped' | null
  readonly title: string
  readonly body: string
  readonly showSupport: boolean
}>()
</script>
