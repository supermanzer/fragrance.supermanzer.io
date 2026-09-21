<template>
  <v-card class="pa-6 w-100" elevation="1" rounded="lg">
    <VFadeTransition mode="out-in">
      <div v-if="pageState === 'checking'" key="checking" class="d-flex flex-column align-center pa-8">
        <v-progress-circular indeterminate color="primary" size="48" width="3" class="mb-4" />
        <p class="text-body-2 text-medium-emphasis">Checking your invitation&hellip;</p>
      </div>

      <div v-else-if="pageState === 'invalid'" key="invalid" class="text-center">
        <v-icon icon="mdi-link-off" size="48" class="text-medium-emphasis mb-4" />
        <p class="text-h6 font-weight-regular mb-2">This invitation link isn't valid</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          Invitations are good for fourteen days, and links can't be reused after that. Your account
          already exists, though: created the moment you were approved, before this invite was even
          sent. Request a password reset with the email address you signed up with, and you can set
          your password that way instead.
        </p>
        <v-btn
          color="primary"
          variant="flat"
          rounded="lg"
          block
          to="/auth/forgot-password"
          class="mb-4"
        >
          Reset your password
        </v-btn>
        <NuxtLink to="/" class="text-body-2 text-primary">Back to home</NuxtLink>
      </div>

      <div v-else-if="pageState === 'used'" key="used" class="text-center">
        <v-icon icon="mdi-account-check-outline" size="48" class="text-medium-emphasis mb-4" />
        <p class="text-h6 font-weight-regular mb-2">This invitation has already been used</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          Looks like you've already set a password with this link. Sign in with your account instead.
        </p>
        <v-btn
          color="primary"
          variant="flat"
          rounded="lg"
          block
          to="/auth/login"
          class="mb-4"
        >
          Sign in
        </v-btn>
        <NuxtLink to="/auth/forgot-password" class="text-body-2 text-primary">Forgot your password?</NuxtLink>
      </div>

      <div v-else-if="pageState === 'precheck-error'" key="precheck-error" class="text-center">
        <v-alert type="error" variant="tonal" class="mb-4">
          We couldn't check your invitation just now. Please try again.
        </v-alert>
        <v-btn variant="flat" color="primary" rounded="lg" block @click="checkToken">
          Try again
        </v-btn>
      </div>

      <div v-else key="form">
        <p class="text-h6 font-weight-regular mb-2">Set your password</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          You're in. Choose a password to finish setting up your account.
        </p>

        <v-form ref="form" novalidate @submit.prevent="submit">
          <v-text-field
            v-model="passwordForm.password"
            label="Password"
            type="password"
            variant="outlined"
            rounded="lg"
            autocomplete="new-password"
            :rules="[required]"
            :error-messages="errors.password"
            hide-details="auto"
            class="mb-4"
            :disabled="submitting"
          />
          <v-text-field
            v-model="passwordForm.confirmPassword"
            label="Confirm password"
            type="password"
            variant="outlined"
            rounded="lg"
            autocomplete="new-password"
            :rules="[required, passwordsMatch]"
            :error-messages="errors.confirmPassword"
            hide-details="auto"
            class="mb-4"
            :disabled="submitting"
          />

          <p class="text-caption text-medium-emphasis mb-4">
            By continuing you agree to the
            <NuxtLink to="/terms" target="_blank" rel="noopener" class="text-primary text-decoration-underline">Terms of Service<span class="d-sr-only"> (opens in a new tab)</span></NuxtLink>
            and
            <NuxtLink to="/privacy" target="_blank" rel="noopener" class="text-primary text-decoration-underline">Privacy Policy<span class="d-sr-only"> (opens in a new tab)</span></NuxtLink>.
          </p>

          <v-alert v-if="errors.general" type="error" variant="tonal" class="mb-4">
            {{ errors.general }}
          </v-alert>

          <v-btn
            type="submit"
            color="primary"
            variant="flat"
            rounded="lg"
            block
            :loading="submitting"
          >
            Set password and continue
          </v-btn>
        </v-form>
      </div>
    </VFadeTransition>
  </v-card>
</template>

<script setup lang="ts">
import { setTokens } from '~/composables/useApi'

definePageMeta({ layout: 'auth', auth: false })

type PageState = 'checking' | 'valid' | 'invalid' | 'used' | 'precheck-error'

interface AcceptErrors {
  password?: string | string[]
  confirmPassword?: string
  general?: string
}

const config = useRuntimeConfig()
const route = useRoute()

const pageState = ref<PageState>('checking')
const submitting = ref(false)
const passwordForm = reactive({ password: '', confirmPassword: '' })
const errors = reactive<AcceptErrors>({})
const form = ref()

const token = computed(() => {
  const raw = route.query.token
  return typeof raw === 'string' ? raw : ''
})

const required = (v: string) => !!v || 'Required'
const passwordsMatch = (v: string) => v === passwordForm.password || "Passwords don't match"

watch(() => passwordForm.password, () => {
  errors.password = undefined
})
watch(() => passwordForm.confirmPassword, () => {
  errors.confirmPassword = undefined
})

async function checkToken(): Promise<void> {
  if (!token.value) {
    pageState.value = 'invalid'
    return
  }

  pageState.value = 'checking'
  try {
    await $fetch('/beta/accept/', {
      baseURL: config.public.apiBase,
      method: 'GET',
      query: { token: token.value },
    })
    pageState.value = 'valid'
  } catch (err: any) {
    if (err?.data?.code === 'already_used') {
      pageState.value = 'used'
    } else if (err?.status === 400 || err?.data?.code === 'invalid_or_expired') {
      pageState.value = 'invalid'
    } else {
      pageState.value = 'precheck-error'
    }
  }
}

onMounted(checkToken)

async function submit(): Promise<void> {
  const { valid } = await form.value.validate()
  if (!valid) return

  errors.password = undefined
  errors.general = undefined
  submitting.value = true
  try {
    const data = await $fetch<{ access: string; refresh: string }>('/beta/accept/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      body: { token: token.value, password: passwordForm.password },
    })
    setTokens(data.access, data.refresh)
    await navigateTo('/billing', { replace: true })
  } catch (err: any) {
    if (err?.data?.code === 'already_used') {
      pageState.value = 'used'
    } else if (err?.data?.code === 'invalid_or_expired') {
      pageState.value = 'invalid'
    } else if (err?.data?.password) {
      errors.password = err.data.password
    } else {
      errors.general = 'Something went wrong. Please try again.'
    }
  } finally {
    submitting.value = false
  }
}
</script>
