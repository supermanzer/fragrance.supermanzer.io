<template>
  <div>
    <h1 class="text-h4 mb-6">Settings</h1>

    <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>
    <v-alert v-if="saved" type="success" class="mb-4">Settings saved.</v-alert>

    <v-card v-if="config" max-width="560">
      <v-card-title>Email Delivery</v-card-title>
      <v-card-text>
        <v-text-field v-model="form.recipient_email" label="Recipient email" type="email" class="mb-2" />
        <v-text-field v-model="form.gmail_user" label="Gmail address (sender)" type="email" class="mb-2" />
        <v-text-field
          v-model="form.gmail_app_password_enc"
          label="Gmail app password"
          type="password"
          placeholder="Leave blank to keep existing password"
          class="mb-2"
        />
      </v-card-text>

      <v-divider />

      <v-card-title class="mt-2">Schedule</v-card-title>
      <v-card-text>
        <v-select
          v-model="form.frequency"
          label="Frequency"
          :items="frequencyOptions"
          class="mb-2"
        />
        <v-text-field
          v-model.number="form.run_hour"
          label="Hour of day (0–23, UTC)"
          type="number"
          :min="0"
          :max="23"
          class="mb-2"
        />
        <v-text-field
          v-if="form.frequency === 'weekly'"
          v-model.number="form.run_day_of_week"
          label="Day of week (0=Sun, 6=Sat)"
          type="number"
          :min="0"
          :max="6"
          class="mb-2"
        />
        <v-text-field
          v-if="form.frequency === 'monthly' || form.frequency === 'yearly'"
          v-model.number="form.run_day_of_month"
          label="Day of month"
          type="number"
          :min="1"
          :max="31"
          class="mb-2"
        />
        <v-text-field
          v-if="form.frequency === 'yearly'"
          v-model.number="form.run_month"
          label="Month (1–12)"
          type="number"
          :min="1"
          :max="12"
          class="mb-2"
        />
      </v-card-text>

      <v-card-actions>
        <v-spacer />
        <v-btn color="primary" :loading="saving" @click="submit">Save Settings</v-btn>
      </v-card-actions>
    </v-card>

    <v-skeleton-loader v-else-if="loading" type="card" />
  </div>
</template>

<script setup lang="ts">
import { useSettings, type FragranceConfigInput } from '~/composables/useSettings'

const { config, loading, error, fetchConfig, updateConfig } = useSettings()
const saving = ref(false)
const saved = ref(false)

const frequencyOptions = [
  { title: 'Weekly', value: 'weekly' },
  { title: 'Monthly', value: 'monthly' },
  { title: 'Yearly', value: 'yearly' },
]

const form = reactive<Partial<FragranceConfigInput>>({
  recipient_email: '',
  gmail_user: '',
  gmail_app_password_enc: '',
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
      gmail_user: config.value.gmail_user,
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
  saved.value = false
  try {
    const payload: Partial<FragranceConfigInput> = { ...form }
    if (!payload.gmail_app_password_enc) delete payload.gmail_app_password_enc
    await updateConfig(payload)
    saved.value = true
  } finally {
    saving.value = false
  }
}
</script>
