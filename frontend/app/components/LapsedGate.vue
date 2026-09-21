<template>
  <VTooltip
    v-if="locked"
    :text="reason"
    :location="location"
    open-on-hover
    open-on-focus
    open-on-click
  >
    <template #activator="{ props: activatorProps }">
      <span
        v-bind="activatorProps"
        class="d-inline-block"
        tabindex="0"
        role="button"
        aria-disabled="true"
        :aria-label="`${label}. ${reason}`"
      >
        <span aria-hidden="true" class="d-inline-block">
          <slot />
        </span>
      </span>
    </template>
  </VTooltip>
  <slot v-else />
</template>

<script setup lang="ts">
withDefaults(defineProps<{
  readonly locked: boolean
  readonly label: string
  readonly reason: string
  readonly location?: 'top' | 'bottom' | 'start' | 'end'
}>(), {
  location: 'bottom',
})
</script>
