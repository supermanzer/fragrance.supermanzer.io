<template>
  <VDialog
    :model-value="modelValue"
    max-width="480"
    :persistent="scheduling"
    aria-labelledby="plan-change-title"
    @update:model-value="onUpdate"
    @after-enter="focusTitle"
  >
    <VCard rounded="lg" elevation="3">
      <VCardTitle
        id="plan-change-title"
        ref="titleRef"
        tabindex="-1"
        class="text-h6 pt-6 px-6 pb-2 text-wrap"
      >
        Switch to {{ target.name }} billing?
      </VCardTitle>

      <VCardText class="px-6">
        <VAlert
          v-if="error"
          type="error"
          variant="tonal"
          rounded="lg"
          class="mb-4"
        >
          {{ error }}
        </VAlert>

        <div class="mb-4">
          <p class="text-overline text-medium-emphasis mb-1">Today</p>
          <p class="text-body-1">{{ todayBody }}</p>
        </div>
        <div>
          <p class="text-overline text-medium-emphasis mb-1">{{ dateLabel }}</p>
          <p class="text-body-1">{{ laterBody }}</p>
        </div>
        <p class="text-caption text-medium-emphasis mt-2">
          You can cancel this change any time before then.
        </p>
      </VCardText>

      <VCardActions class="px-6 pb-6 flex-column flex-sm-row ga-2">
        <VSpacer class="d-none d-sm-block" />
        <VBtn
          variant="text"
          rounded="lg"
          height="44"
          class="order-2 order-sm-1 w-100 w-sm-auto"
          :disabled="scheduling"
          @click="emit('dismiss')"
        >
          Not now
        </VBtn>
        <VBtn
          color="primary"
          variant="flat"
          rounded="lg"
          height="44"
          class="order-1 order-sm-2 w-100 w-sm-auto ml-0"
          :loading="scheduling"
          @click="emit('confirm')"
        >
          Schedule the change
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<script setup lang="ts">
import type { Plan } from '~/composables/useBilling'

const props = defineProps<{
  readonly modelValue: boolean
  readonly target: Plan
  readonly currentName: string
  readonly date: string | null
  readonly scheduling: boolean
  readonly error: string | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'confirm'): void
  (e: 'dismiss'): void
}>()

const titleRef = ref<{ $el: HTMLElement } | null>(null)

const toAnnual = computed(() => props.target.billing_interval === 'year')
const price = computed(() => formatPlanPrice(props.target))
const dateText = computed(() => (props.date ? formatExpiryDate(props.date) : null))
const dateLabel = computed(() => (dateText.value ? `On ${dateText.value}` : 'When your current period ends'))

const todayBody = computed(() =>
  toAnnual.value
    ? `Nothing is charged. You stay on your ${props.currentName} plan exactly as it is.`
    : `Nothing is charged. You keep your ${props.currentName} term in full, with no refund or credit needed.`,
)

const laterBody = computed(() =>
  toAnnual.value
    ? `You're charged ${price.value} for a year, and your plan becomes ${props.target.name}. Your access carries on without a gap.`
    : `Your ${props.currentName} term ends, you're charged ${price.value} a month, and your plan becomes ${props.target.name}. Your access carries on without a gap.`,
)

function focusTitle(): void {
  titleRef.value?.$el?.focus()
}

// Esc / scrim click arrive here as false; route them through the same dismiss path as "Not now".
function onUpdate(value: boolean): void {
  if (!value) emit('dismiss')
  else emit('update:modelValue', value)
}
</script>
