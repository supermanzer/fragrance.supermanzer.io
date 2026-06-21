<template>
  <div>
    <h1 class="text-h4 mb-6">Settings</h1>

    <AppAlert v-model="error" class="mb-4" />
    <AppAlert v-model="saved" type="success" class="mb-4" />

    <v-skeleton-loader v-if="loading" type="card" max-width="560" />

    <v-card v-else max-width="560" elevation="1" rounded="lg">
      <v-card-title class="pt-6 px-6 pb-2">Schedule</v-card-title>
      <v-card-text class="px-6 pb-2">
        <v-text-field
          v-model="form.recipient_email"
          label="Recipient email"
          type="email"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
          class="mb-4"
        />
        <v-select
          v-model="form.frequency"
          label="Frequency"
          :items="frequencyOptions"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
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
          class="mb-4"
        />
        <v-text-field
          v-if="form.frequency === 'weekly'"
          v-model.number="form.run_day_of_week"
          label="Day of week (0=Sun, 6=Sat)"
          type="number"
          :min="0"
          :max="6"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
          class="mb-4"
        />
        <v-text-field
          v-if="form.frequency === 'monthly' || form.frequency === 'yearly'"
          v-model.number="form.run_day_of_month"
          label="Day of month"
          type="number"
          :min="1"
          :max="31"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
          class="mb-4"
        />
        <v-text-field
          v-if="form.frequency === 'yearly'"
          v-model.number="form.run_month"
          label="Month (1–12)"
          type="number"
          :min="1"
          :max="12"
          variant="outlined"
          rounded="lg"
          hide-details="auto"
          class="mb-4"
        />
      </v-card-text>
      <v-card-actions class="px-6 pb-6 pt-2">
        <v-spacer />
        <v-btn color="primary" variant="flat" rounded="lg" :loading="saving" @click="submit">
          Save Settings
        </v-btn>
      </v-card-actions>
    </v-card>

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
          @update:model-value="importReset()"
        />
        <AppAlert v-model="importError" class="mt-4" />
        <AppAlert v-model="importResult" type="success" class="mt-4">
          Import complete —
          {{ importResult?.created }} added,
          {{ importResult?.updated }} updated,
          {{ importResult?.skipped }} skipped.
        </AppAlert>
      </v-card-text>
      <v-card-actions class="px-6 pb-6 pt-4">
        <v-spacer />
        <v-btn
          color="primary"
          variant="flat"
          rounded="lg"
          prepend-icon="mdi-upload"
          :loading="importLoading"
          :disabled="!importFile"
          @click="runImport"
        >
          Upload
        </v-btn>
      </v-card-actions>
    </v-card>
  </div>
</template>

<script setup lang="ts">
import { useSettings, type FragranceConfigInput } from '~/composables/useSettings'
import { useImport } from '~/composables/useImport'
import { useExport } from '~/composables/useExport'
import { useApi, clearTokens } from '~/composables/useApi'

const { config, loading, error, fetchConfig, updateConfig } = useSettings()
const { api } = useApi()
const saving = ref(false)
const saved = ref<string | null>(null)

const { loading: importLoading, error: importError, result: importResult, importCollection, reset: importReset } = useImport()
const importFile = ref<File | null>(null)

const { loading: exportLoading, error: exportError, exportCollection, reset: exportReset } = useExport()

async function runExport(): Promise<void> {
  exportReset()
  await exportCollection()
}

async function runImport(): Promise<void> {
  const file = importFile.value
  if (!file) return
  await importCollection(file)
  if (importResult.value) importFile.value = null
}

const frequencyOptions = [
  { title: 'Weekly', value: 'weekly' },
  { title: 'Monthly', value: 'monthly' },
  { title: 'Yearly', value: 'yearly' },
]

const form = reactive<Partial<FragranceConfigInput>>({
  recipient_email: '',
  frequency: 'monthly',
  run_hour: 9,
  run_day_of_week: 1,
  run_day_of_month: 1,
  run_month: 1,
})

onMounted(async () => {
  await fetchConfig()
  if (config.value) {
    Object.assign(form, {
      recipient_email: config.value.recipient_email,
      frequency: config.value.frequency,
      run_hour: config.value.run_hour,
      run_day_of_week: config.value.run_day_of_week,
      run_day_of_month: config.value.run_day_of_month,
      run_month: config.value.run_month,
    })
  }
})

async function submit() {
  saving.value = true
  saved.value = null
  try {
    await updateConfig({ ...form })
    saved.value = 'Settings saved.'
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
