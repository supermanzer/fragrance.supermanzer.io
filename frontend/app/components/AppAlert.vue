<template>
  <v-alert
    v-if="show"
    :type="type"
    :variant="variant"
    :rounded="rounded"
    closable
    @click:close="dismiss"
  >
    <slot>{{ autoMessage }}</slot>
  </v-alert>
</template>

<script setup lang="ts">
const props = withDefaults(defineProps<{
  modelValue: unknown
  type?: 'error' | 'success' | 'info' | 'warning'
  variant?: string
  rounded?: string
}>(), {
  type: 'error',
  variant: 'tonal',
  rounded: 'lg',
})

const emit = defineEmits<{
  'update:modelValue': [value: null]
}>()

const show = computed(() => !!props.modelValue)
const autoMessage = computed(() => typeof props.modelValue === 'string' ? props.modelValue : null)

function dismiss() {
  emit('update:modelValue', null)
}
</script>
