<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
    <p class="text-h6 font-weight-regular mb-6">Set a new password</p>

    <v-alert v-if="linkInvalid" type="error" variant="tonal" class="mb-4">
      This reset link is invalid or has expired. Please
      <NuxtLink to="/auth/forgot-password" class="text-primary">request a new one</NuxtLink>.
    </v-alert>

    <v-form v-else-if="!done" @submit.prevent="submit">
      <v-text-field
        v-model="form.new_password"
        label="New password"
        type="password"
        variant="outlined"
        rounded="lg"
        autocomplete="new-password"
        :error-messages="errors.new_password"
        hide-details="auto"
        class="mb-4"
        required
      />
      <v-text-field
        v-model="form.new_password_confirm"
        label="Confirm new password"
        type="password"
        variant="outlined"
        rounded="lg"
        autocomplete="new-password"
        :error-messages="errors.new_password_confirm"
        hide-details="auto"
        class="mb-6"
        required
      />

      <v-alert v-if="errors.general" type="error" variant="tonal" class="mb-4">
        {{ errors.general }}
      </v-alert>

      <v-btn type="submit" color="primary" variant="flat" rounded="lg" block :loading="loading">
        Set Password
      </v-btn>
    </v-form>

    <div v-else>
      <v-alert type="success" variant="tonal" class="mb-4">
        Your password has been reset. You can now sign in with your new password.
      </v-alert>
      <v-btn color="primary" variant="flat" rounded="lg" block @click="navigateTo('/auth/login')">
        Sign In
      </v-btn>
    </div>
  </v-card>
</template>

<script setup lang="ts">
definePageMeta({ layout: 'auth' })

const config = useRuntimeConfig()
const route = useRoute()

const uid = route.query.uid as string | undefined
const token = route.query.token as string | undefined
const linkInvalid = !uid || !token

const form = reactive({ new_password: '', new_password_confirm: '' })
const errors = reactive<Record<string, string | string[]>>({})
const loading = ref(false)
const done = ref(false)

async function submit() {
  Object.keys(errors).forEach(k => delete errors[k])
  loading.value = true
  try {
    await $fetch('/auth/password/reset/confirm/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { uid, token, ...form },
    })
    done.value = true
  } catch (err: any) {
    const data = err?.data
    if (data?.new_password) errors.new_password = data.new_password
    else if (data?.new_password_confirm) errors.new_password_confirm = data.new_password_confirm
    else errors.general = data?.detail ?? data?.non_field_errors?.[0] ?? 'Something went wrong. Please try again.'
  } finally {
    loading.value = false
  }
}
</script>
