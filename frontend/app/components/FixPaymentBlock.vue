<template>
  <div :class="prominent ? 'mb-8' : 'mt-6'">
    <VAlert
      v-if="error"
      type="error"
      variant="tonal"
      rounded="lg"
      class="mb-4"
    >
      {{ error }}
    </VAlert>
    <div class="d-flex flex-column flex-sm-row align-sm-center ga-4">
      <div class="flex-grow-1">
        <p class="text-subtitle-1 mb-1">{{ prominent ? "Your last payment didn't go through" : 'Had a payment problem?' }}</p>
        <p class="text-body-2 text-medium-emphasis mb-0">
          Update your card in Stripe and we'll retry the payment for you. If it goes through, your access comes back on its own.
        </p>
      </div>
      <VBtn
        :variant="prominent ? 'flat' : 'text'"
        color="primary"
        rounded="lg"
        height="44"
        prepend-icon="mdi-credit-card-outline"
        class="flex-shrink-0"
        :block="$vuetify.display.xs"
        :loading="loading"
        :disabled="loading"
        @click="emit('fix')"
      >
        Fix payment
      </VBtn>
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  readonly prominent: boolean
  readonly loading: boolean
  readonly error: string | null
}>()

const emit = defineEmits<{ fix: [] }>()
</script>
