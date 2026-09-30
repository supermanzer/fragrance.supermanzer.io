<template>
  <div>
    <h1 class="text-h4 mb-6">Settings</h1>

    <AppAlert v-model="error" class="mb-4" />
    <AppAlert v-model="readOnlyNotice" type="info" class="mb-4" />
    <AppAlert v-model="saved" type="success" class="mb-4" />

    <v-skeleton-loader v-if="loading" type="card" max-width="560" />

    <v-card v-else max-width="560" elevation="1" rounded="lg">
      <v-card-title class="pt-6 px-6 pb-2">Schedule</v-card-title>
      <v-card-text class="px-6 pb-2">
        <p v-if="isLapsed" class="text-caption text-medium-emphasis mb-4">
          Schedule changes need an active subscription.
        </p>
        <VExpandTransition>
          <div v-if="legacyFrequency">
            <v-alert type="info" variant="tonal" density="compact" rounded="lg" class="mb-4">
              Your schedule was set to {{ legacyFrequency }}. Picks are now made monthly, and we'll switch you when you save.
            </v-alert>
          </div>
        </VExpandTransition>
        <p class="text-body-2 mb-4">Your picks are made once a month.</p>
        <v-text-field
          v-model="form.recipient_email"
          label="Recipient email"
          type="email"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
          :readonly="isLapsed"
          class="mb-4"
        />
        <v-text-field
          v-model.number="form.run_day_of_month"
          label="Day of month"
          type="number"
          :min="1"
          :max="28"
          variant="outlined"
          rounded="lg"
          hint="Runs happen on this day each month, at the hour below."
          persistent-hint
          :readonly="isLapsed"
          class="mb-4"
        />
        <v-text-field
          v-model.number="form.run_hour"
          label="Hour of day (0–23, UTC)"
          type="number"
          :min="0"
          :max="23"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
          :readonly="isLapsed"
          class="mb-4"
        />
      </v-card-text>
      <v-card-actions class="px-6 pb-6 pt-2">
        <v-spacer />
        <LapsedGate
          :locked="isLapsed"
          label="Save Settings"
          reason="Changing your schedule needs an active subscription."
        >
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            :loading="saving"
            :disabled="isLapsed"
            @click="submit"
          >
            Save Settings
          </v-btn>
        </LapsedGate>
      </v-card-actions>
    </v-card>

    <div v-if="known && !isExempt" class="mt-8" style="max-width: 560px">
      <p class="text-h6 font-weight-regular mb-1">Subscription</p>
      <p class="text-body-2 text-medium-emphasis mb-3">Change plans, update your card, or see invoices.</p>
      <v-btn variant="text" color="primary" rounded="lg" to="/billing">Go to billing</v-btn>
    </div>

    <!-- Change password -->
    <v-card class="mt-6" max-width="560" elevation="1" rounded="lg">
      <v-card-title class="pt-6 px-6 pb-2">Change Password</v-card-title>
      <v-card-text class="px-6 pb-2">
        <v-text-field
          v-model="passwordForm.current_password"
          label="Current password"
          type="password"
          variant="outlined"
          rounded="lg"
          autocomplete="current-password"
          :error-messages="passwordErrors.current_password"
          hide-details="auto"
          class="mb-4"
        />
        <v-text-field
          v-model="passwordForm.new_password"
          label="New password"
          type="password"
          variant="outlined"
          rounded="lg"
          autocomplete="new-password"
          :error-messages="passwordErrors.new_password"
          hide-details="auto"
          class="mb-4"
        />
        <v-text-field
          v-model="passwordForm.new_password_confirm"
          label="Confirm new password"
          type="password"
          variant="outlined"
          rounded="lg"
          autocomplete="new-password"
          :error-messages="passwordErrors.new_password_confirm"
          hide-details="auto"
          class="mb-2"
        />
        <AppAlert v-model="passwordError" class="mt-4" />
        <AppAlert v-model="passwordSaved" type="success" class="mt-4" />
      </v-card-text>
      <v-card-actions class="px-6 pb-6 pt-2">
        <v-spacer />
        <v-btn color="primary" variant="flat" rounded="lg" :loading="passwordSaving" @click="changePassword">
          Update Password
        </v-btn>
      </v-card-actions>
    </v-card>

    <!-- Collection data: export + import -->
    <v-card class="mt-6" max-width="560" elevation="1" rounded="lg">
      <v-card-title class="pt-6 px-6 pb-1">Collection Data</v-card-title>
      <v-card-text class="px-6 pb-2">

        <!-- Export row — always available, so it sits above the file-gated import -->
        <div class="d-flex align-center justify-space-between mb-6">
          <div>
            <div class="text-body-2">Export Collection</div>
            <div class="text-caption text-medium-emphasis">
              Download your full collection as a CSV file.
            </div>
          </div>
          <v-btn
            color="primary"
            variant="outlined"
            rounded="lg"
            prepend-icon="mdi-download"
            :loading="exportLoading"
            @click="runExport"
          >
            Download
          </v-btn>
        </div>

        <AppAlert v-model="exportError" class="mb-4" />

        <v-divider class="mb-6" />

        <!-- Import section -->
        <div class="text-body-2 mb-1">Import Collection</div>
        <div class="text-caption text-medium-emphasis mb-4">
          Upload a CSV to add or update fragrances in bulk.<br>
          Required columns: <code>fragrance, status, house, notes</code> —
          status must be <code>own</code>, <code>like</code>, or <code>dislike</code>.
        </div>
        <v-file-input
          v-model="importFile"
          label="CSV file"
          accept=".csv"
          variant="outlined"
          rounded="lg"
          show-size
          prepend-icon=""
          prepend-inner-icon="mdi-file-delimited-outline"
          hide-details="auto"
          :disabled="isLapsed"
          @update:model-value="importReset()"
        />
        <p v-if="isLapsed" class="text-caption text-medium-emphasis mt-2">
          Importing needs an active subscription.
        </p>
        <AppAlert v-model="importError" class="mt-4" />
        <AppAlert v-model="importReadOnly" type="info" class="mt-4" />
        <AppAlert v-model="importResult" type="success" class="mt-4">
          Import complete —
          {{ importResult?.created }} added,
          {{ importResult?.updated }} updated,
          {{ importResult?.skipped }} skipped.
        </AppAlert>
      </v-card-text>
      <v-card-actions class="px-6 pb-6 pt-4">
        <v-spacer />
        <LapsedGate
          :locked="isLapsed"
          label="Upload"
          reason="Importing needs an active subscription."
        >
          <v-btn
            color="primary"
            variant="flat"
            rounded="lg"
            prepend-icon="mdi-upload"
            :loading="importLoading"
            :disabled="!importFile || isLapsed"
            @click="runImport"
          >
            Upload
          </v-btn>
        </LapsedGate>
      </v-card-actions>
    </v-card>
  </div>
</template>

<script setup lang="ts">
import { useSettings } from '~/composables/useSettings'
import { useImport } from '~/composables/useImport'
import { useExport } from '~/composables/useExport'
import { useApi, clearTokens, isSubscriptionRequired } from '~/composables/useApi'

const { config, loading, error, fetchConfig, updateConfig } = useSettings()
const { api } = useApi()
const resetSessionState = useSessionReset()
const saving = ref(false)
const saved = ref<string | null>(null)

const { isLapsed, known, isExempt } = useSubscription()
const readOnlyNotice = ref<string | null>(null)

const { loading: importLoading, error: importError, readOnly: importReadOnly, result: importResult, importCollection, reset: importReset } = useImport()
const importFile = ref<File | null>(null)

const { loading: exportLoading, error: exportError, exportCollection, reset: exportReset } = useExport()

async function runExport(): Promise<void> {
  exportReset()
  await exportCollection()
}

async function runImport(): Promise<void> {
  const file = importFile.value
  if (!file || isLapsed.value) return
  await importCollection(file)
  if (importResult.value) importFile.value = null
}

const form = reactive<{
  recipient_email: string
  run_hour: number
  run_day_of_month: number
}>({
  recipient_email: '',
  run_hour: 9,
  run_day_of_month: 1,
})

const legacyFrequency = computed(() => {
  const f = config.value?.frequency
  return f === 'weekly' || f === 'yearly' ? f : null
})

onMounted(async () => {
  await fetchConfig()
  if (config.value) {
    const day = config.value.run_day_of_month
    Object.assign(form, {
      recipient_email: config.value.recipient_email,
      run_hour: config.value.run_hour,
      run_day_of_month: Number.isInteger(day) && day >= 1 && day <= 28 ? day : 1,
    })
  }
})

async function submit() {
  saving.value = true
  saved.value = null
  error.value = null
  readOnlyNotice.value = null
  try {
    await updateConfig({
      recipient_email: form.recipient_email,
      frequency: 'monthly',
      run_hour: form.run_hour,
      run_day_of_month: form.run_day_of_month,
    })
    saved.value = 'Settings saved.'
  } catch (err: unknown) {
    if (isSubscriptionRequired(err)) {
      readOnlyNotice.value = READ_ONLY_WRITE_NOTICE
      return
    }
    const e = err as { status?: number; data?: Record<string, unknown> }
    if (e?.status === 400) {
      error.value = e.data?.frequency
        ? 'Only monthly schedules are available. Please reload the page and try again.'
        : "We couldn't save your schedule. Please check the day and hour and try again."
    } else {
      error.value = "We couldn't save your settings just now. Please try again in a moment."
    }
  } finally {
    saving.value = false
  }
}

const passwordForm = reactive({ current_password: '', new_password: '', new_password_confirm: '' })
const passwordErrors = reactive<Record<string, string | string[]>>({})
const passwordError = ref<string | null>(null)
const passwordSaved = ref<string | null>(null)
const passwordSaving = ref(false)

async function changePassword() {
  Object.keys(passwordErrors).forEach(k => delete passwordErrors[k])
  passwordError.value = null
  passwordSaved.value = null
  passwordSaving.value = true
  try {
    const refresh = localStorage.getItem('auth_refresh') ?? ''
    await api('/auth/password/change/', {
      method: 'POST',
      body: { ...passwordForm, refresh },
    })
    passwordSaved.value = 'Password updated. Please sign in again.'
    Object.keys(passwordForm).forEach(k => (passwordForm as any)[k] = '')
    clearTokens()
    resetSessionState()
    setTimeout(() => navigateTo('/auth/login'), 1500)
  } catch (err: any) {
    const data = err?.data
    if (data?.current_password) passwordErrors.current_password = data.current_password
    else if (data?.new_password) passwordErrors.new_password = data.new_password
    else if (data?.new_password_confirm) passwordErrors.new_password_confirm = data.new_password_confirm
    else passwordError.value = data?.detail ?? 'Something went wrong. Please try again.'
  } finally {
    passwordSaving.value = false
  }
}
</script>
