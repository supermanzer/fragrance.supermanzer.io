<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
    <p class="text-h6 font-weight-regular mb-2">Forgot your password?</p>
    <p class="text-body-2 text-medium-emphasis mb-6">
      Enter your email address and we'll send you a reset link.
    </p>

    <v-form v-if="!sent" @submit.prevent="submit">
      <v-text-field
        v-model="email"
        label="Email address"
        type="email"
        variant="outlined"
        rounded="lg"
        autocomplete="email"
        hide-details="auto"
        class="mb-6"
        required
      />

      <v-alert v-if="error" type="error" variant="tonal" class="mb-4">
        {{ error }}
      </v-alert>

      <v-btn type="submit" color="primary" variant="flat" rounded="lg" block :loading="loading">
        Send Reset Link
      </v-btn>

      <div class="text-center mt-4">
        <NuxtLink to="/auth/login" class="text-body-2 text-primary">Back to sign in</NuxtLink>
      </div>
    </v-form>

    <div v-else>
      <v-alert type="success" variant="tonal" class="mb-4">
        If an account with that email exists, a reset link has been sent. Check your inbox.
      </v-alert>
      <div class="text-center mt-4">
        <NuxtLink to="/auth/login" class="text-body-2 text-primary">Back to sign in</NuxtLink>
      </div>
    </div>
  </v-card>
</template>

<script setup lang="ts">
definePageMeta({ layout: 'auth' })

const config = useRuntimeConfig()
const email = ref('')
const loading = ref(false)
const error = ref('')
const sent = ref(false)

async function submit() {
  error.value = ''
  loading.value = true
  try {
    await $fetch('/auth/password/reset/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { email: email.value },
    })
    sent.value = true
  } catch {
    error.value = 'Something went wrong. Please try again.'
  } finally {
    loading.value = false
  }
}
</script>
