<template>
  <v-container class="fill-height" max-width="400">
    <v-card class="pa-6 w-100">
      <v-card-title class="mb-4">Create Account</v-card-title>

      <v-form @submit.prevent="submit">
        <v-text-field
          v-model="form.username"
          label="Username"
          autocomplete="username"
          :error-messages="errors.username"
          required
        />
        <v-text-field
          v-model="form.email"
          label="Email"
          type="email"
          autocomplete="email"
          :error-messages="errors.email"
          required
        />
        <v-text-field
          v-model="form.password"
          label="Password"
          type="password"
          autocomplete="new-password"
          :error-messages="errors.password"
          required
        />

        <v-alert v-if="errors.general" type="error" class="mb-4">
          {{ errors.general }}
        </v-alert>

        <v-btn type="submit" color="primary" block :loading="loading">
          Create Account
        </v-btn>
      </v-form>

      <v-card-text class="text-center mt-4">
        <NuxtLink to="/auth/login">Already have an account?</NuxtLink>
      </v-card-text>
    </v-card>
  </v-container>
</template>

<script setup lang="ts">
import { setTokens } from '~/composables/useApi'

definePageMeta({ layout: false })

const config = useRuntimeConfig()

const form = reactive({ username: '', email: '', password: '' })
const errors = reactive<Record<string, string>>({})
const loading = ref(false)

async function submit() {
  Object.keys(errors).forEach(k => delete errors[k])
  loading.value = true
  try {
    const data = await $fetch<{ access: string; refresh: string }>('/auth/register/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { username: form.username, email: form.email, password: form.password },
    })
    setTokens(data.access, data.refresh)
    await navigateTo('/fragrance')
  } catch (err: any) {
    const detail = err?.data
    if (typeof detail === 'object') {
      Object.assign(errors, detail)
    } else {
      errors.general = 'Registration failed. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
</script>
