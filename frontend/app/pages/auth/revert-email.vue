<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
    <div aria-live="polite">
      <VFadeTransition mode="out-in">
        <div v-if="pageState === 'confirm'" key="confirm">
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            Wasn't you? Restore your previous email
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-6">
            Someone changed the email on your account through billing management. Restoring it
            signs out every device, and you should then reset your password.
          </p>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            :loading="loading"
            :disabled="loading"
            @click="restore"
          >
            Restore my previous email
          </v-btn>
          <div class="text-center mt-4">
            <NuxtLink to="/" class="text-body-2 text-primary">Cancel</NuxtLink>
          </div>
        </div>

        <div v-else-if="pageState === 'success'" key="success" class="text-center">
          <v-icon icon="mdi-check-circle-outline" size="48" color="success" class="mb-4" aria-hidden="true" />
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            Your previous email has been restored
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-2">
            Your previous email has been restored and all devices have been signed out.
          </p>
          <p class="text-body-2 mb-6">Reset your password now to secure your account.</p>
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            block
            size="large"
            to="/auth/forgot-password"
          >
            Reset your password
          </v-btn>
        </div>

        <div v-else-if="pageState === 'invalid'" key="invalid" class="text-center">
          <v-icon icon="mdi-link-off" size="48" class="text-medium-emphasis mb-4" aria-hidden="true" />
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-2">
            This link is invalid or has expired.
          </h1>
          <p class="text-body-2 text-medium-emphasis mb-6">
            If you still need help, contact support.
          </p>
          <NuxtLink to="/" class="text-body-2 text-primary">Back to home</NuxtLink>
        </div>

        <div v-else key="error">
          <h1 ref="heading" tabindex="-1" class="text-h6 font-weight-regular mb-4">
            Something went wrong
          </h1>
          <v-alert type="error" variant="tonal" class="mb-4">
            Something went wrong. Please try again in a minute.
          </v-alert>
          <v-btn color="primary" variant="flat" rounded="lg" block :loading="loading" @click="restore">
            Try again
          </v-btn>
          <div class="text-center mt-4">
            <NuxtLink to="/" class="text-body-2 text-primary">Cancel</NuxtLink>
          </div>
        </div>
      </VFadeTransition>
    </div>
  </v-card>
</template>

<script setup lang="ts">
import { clearTokens } from '~/composables/useApi'

definePageMeta({ layout: 'auth', auth: false })
useSeoMeta({ title: 'Restore your previous email', robots: 'noindex' })

type PageState = 'confirm' | 'success' | 'invalid' | 'error'

const config = useRuntimeConfig()
const route = useRoute()
const resetSessionState = useSessionReset()
const { authStatus } = useAuthStatus()

const token = computed(() => {
  const raw = route.query.token
  return typeof raw === 'string' ? raw.trim() : ''
})

const pageState = ref<PageState>(token.value ? 'confirm' : 'invalid')
const loading = ref(false)
const heading = ref<HTMLElement | null>(null)

watch(pageState, async () => {
  await nextTick()
  // Wait for the out-in fade to mount the new block before focusing.
  setTimeout(() => heading.value?.focus(), 250)
})

async function restore(): Promise<void> {
  if (loading.value) return
  if (!token.value) {
    pageState.value = 'invalid'
    return
  }
  loading.value = true
  try {
    await $fetch('/auth/revert-email/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { token: token.value },
    })
    clearTokens()
    resetSessionState()
    authStatus.value = 'unauthenticated'
    pageState.value = 'success'
  } catch (err: any) {
    pageState.value = err?.status === 400 ? 'invalid' : 'error'
  } finally {
    loading.value = false
  }
}
</script>
