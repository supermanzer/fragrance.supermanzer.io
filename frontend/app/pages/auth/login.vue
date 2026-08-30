<template>
  <div v-if="checking || authStatus === 'authenticated'" class="d-flex justify-center pa-8">
    <v-progress-circular indeterminate color="primary" />
  </div>

  <v-card v-else class="pa-6 w-100" elevation="1" rounded="lg">
    <p class="text-h6 font-weight-regular mb-6">Sign in to your account</p>

    <v-form @submit.prevent="submit">
      <v-text-field
        v-model="form.username"
        label="Username"
        variant="outlined"
        rounded="lg"
        autocomplete="username"
        :error-messages="errors.username"
        hide-details="auto"
        class="mb-4"
        required
      />
      <v-text-field
        v-model="form.password"
        label="Password"
        type="password"
        variant="outlined"
        rounded="lg"
        autocomplete="current-password"
        :error-messages="errors.password"
        hide-details="auto"
        class="mb-6"
        required
      />

      <v-alert v-if="errors.general" type="error" variant="tonal" class="mb-4">
        {{ errors.general }}
      </v-alert>

      <v-btn type="submit" color="primary" variant="flat" rounded="lg" block :loading="loading">
        Sign In
      </v-btn>

      <div class="text-center mt-4">
        <NuxtLink to="/auth/forgot-password" class="text-body-2 text-primary">Forgot your password?</NuxtLink>
      </div>
    </v-form>

  </v-card>
</template>

<script setup lang="ts">
import { setTokens } from '~/composables/useApi'

definePageMeta({ layout: 'auth', auth: false })

const config = useRuntimeConfig()

const form = reactive({ username: '', password: '' })
const errors = reactive<Record<string, string>>({})
const loading = ref(false)

const { authStatus, checkAuth } = useAuthStatus()
// This page's own render gate, distinct from authStatus: 'unknown' can mean
// either "haven't checked yet" or "checked, but the result was indeterminate"
// (see useAuthStatus.ts). This page's job is letting people sign in, so an
// indeterminate result must fall through to the form, not hang on a spinner
// forever waiting for a resolution that a 429 or network blip may never
// deliver. Only suppress the form while a check is actually in flight, or
// once we've affirmatively confirmed 'authenticated' (mid-redirect).
const checking = ref(true)

onMounted(async () => {
  try {
    await checkAuth()
    if (authStatus.value === 'authenticated') {
      await navigateTo('/fragrance', { replace: true })
      return
    }
  } finally {
    checking.value = false
  }
})

async function submit() {
  Object.keys(errors).forEach(k => delete errors[k])
  loading.value = true
  try {
    const data = await $fetch<{ access: string; refresh: string }>('/auth/token/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { username: form.username, password: form.password },
    })
    setTokens(data.access, data.refresh)
    await navigateTo('/fragrance')
  } catch (err: any) {
    const detail = err?.data?.detail
    errors.general = detail ?? 'Login failed. Please check your credentials.'
  } finally {
    loading.value = false
  }
}
</script>
