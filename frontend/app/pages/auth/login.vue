<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
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
    </v-form>

    <div class="text-center mt-4">
      <v-btn variant="text" size="small" :to="'/auth/register'">
        Create an account
      </v-btn>
    </div>
  </v-card>
</template>

<script setup lang="ts">
import { setTokens } from '~/composables/useApi'

definePageMeta({ layout: 'auth' })

const config = useRuntimeConfig()

const form = reactive({ username: '', password: '' })
const errors = reactive<Record<string, string>>({})
const loading = ref(false)

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
