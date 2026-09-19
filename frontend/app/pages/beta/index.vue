<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
    <p class="text-h6 font-weight-regular mb-2">Join the beta</p>
    <p class="text-body-2 text-medium-emphasis mb-6">
      Fragrances is in a small, closed beta. Leave your email and we'll be in touch if a place opens up.
    </p>

    <VFadeTransition mode="out-in">
      <v-form v-if="!sent" key="form" ref="form" novalidate @submit.prevent="submit">
        <v-text-field
          v-model="email"
          label="Email address"
          type="email"
          variant="outlined"
          rounded="lg"
          autocomplete="email"
          :rules="[required, validEmail]"
          :error-messages="errors.email"
          hide-details="auto"
          class="mb-6"
          :disabled="loading"
        />

        <input
          v-model="website"
          type="text"
          name="website"
          tabindex="-1"
          autocomplete="off"
          aria-hidden="true"
          style="position: absolute; left: -9999px; top: -9999px; width: 1px; height: 1px; overflow: hidden;"
        />

        <v-alert v-if="errors.general" type="error" variant="tonal" class="mb-4">
          {{ errors.general }}
        </v-alert>

        <v-btn
          type="submit"
          color="primary"
          variant="flat"
          rounded="lg"
          block
          :loading="loading"
          :disabled="loading"
        >
          Join the waitlist
        </v-btn>

        <div class="text-center mt-4">
          <NuxtLink to="/auth/login" class="text-body-2 text-primary">Already have an account? Sign in</NuxtLink>
        </div>
        <div class="text-center mt-2">
          <NuxtLink to="/" class="text-body-2 text-primary">Back to home</NuxtLink>
        </div>
      </v-form>

      <div v-else key="success">
        <v-alert type="success" variant="tonal" class="mb-4">
          We'll be in touch by email if there's a place for you.
        </v-alert>
        <div class="text-center mt-4">
          <NuxtLink to="/" class="text-body-2 text-primary">Back to home</NuxtLink>
        </div>
      </div>
    </VFadeTransition>
  </v-card>
</template>

<script setup lang="ts">
definePageMeta({ layout: 'auth', auth: false })

interface BetaErrors {
  email?: string | string[]
  general?: string
}

const config = useRuntimeConfig()

const email = ref('')
const website = ref('')
const loading = ref(false)
const sent = ref(false)
const errors = reactive<BetaErrors>({})
const form = ref()

const required = (v: string) => !!v || 'Required'
const validEmail = (v: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v) || 'Enter a valid email address'

// :error-messages overrides Vuetify's :rules validation state, so a stale
// server-set errors.email would otherwise keep the field (and form.validate())
// stuck in an error state after the user corrects it — clear it on input and
// let :rules re-derive the field's own validity from what's typed now.
watch(email, () => {
  errors.email = undefined
})

async function submit(): Promise<void> {
  const { valid } = await form.value.validate()
  if (!valid) return

  errors.email = undefined
  errors.general = undefined
  loading.value = true
  try {
    await $fetch('/beta/signup/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { email: email.value, website: website.value },
    })
    sent.value = true
  } catch (err: any) {
    if (err?.data?.email) {
      errors.email = err.data.email
    } else if (err?.status === 429 || err?.status === 503) {
      errors.general = 'Too many attempts. Please wait a moment and try again.'
    } else {
      errors.general = 'Something went wrong. Please try again.'
    }
  } finally {
    loading.value = false
  }
}
</script>
