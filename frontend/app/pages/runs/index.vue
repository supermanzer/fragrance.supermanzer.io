<template>
  <div>
    <div class="d-flex align-center mb-6">
      <h1 class="text-h4">Recommendation Runs</h1>
      <v-spacer />
      <v-btn color="primary" variant="flat" rounded="lg" prepend-icon="mdi-play" :loading="triggering" @click="trigger">
        Run Now
      </v-btn>
    </div>

    <AppAlert v-if="triggerError" v-model="triggerError" class="mb-4" />
    <AppAlert v-else-if="triggered" v-model="triggered" type="success" class="mb-4" />
    <AppAlert v-if="error" v-model="error" class="mb-4" />

    <v-skeleton-loader v-if="loading" type="card@3" />

    <div v-else-if="!runs.length" class="text-center py-12">
      <v-icon size="56" class="text-medium-emphasis mb-4">mdi-flask-outline</v-icon>
      <p class="text-body-1 text-medium-emphasis">No runs yet.</p>
      <p class="text-body-2 text-medium-emphasis mt-1">Click "Run Now" to generate your first recommendations.</p>
    </div>

    <v-expansion-panels v-else variant="accordion">
      <v-expansion-panel v-for="run in runs" :key="run.id">
        <v-expansion-panel-title>
          <div class="d-flex align-center w-100 mr-4">
            <span class="text-body-2">{{ new Date(run.triggered_at).toLocaleString() }}</span>
            <v-chip :color="statusColor(run.status)" size="small" class="ml-4">{{ run.status }}</v-chip>
            <v-spacer />
            <span class="text-caption text-medium-emphasis">{{ run.picks.length }} pick(s)</span>
          </div>
        </v-expansion-panel-title>

        <v-expansion-panel-text>
          <p v-if="run.intro" class="text-body-2 font-italic mb-6" style="max-width: 680px">{{ run.intro }}</p>

          <v-alert v-if="run.error_message" type="error" variant="tonal" class="mb-4">
            {{ run.error_message }}
          </v-alert>

          <div v-if="run.picks.length" class="d-flex flex-column ga-3">
            <v-card
              v-for="pick in run.picks"
              :key="pick.id"
              elevation="1"
              rounded="lg"
            >
              <v-card-text class="pa-4">
                <div class="d-flex align-start justify-space-between mb-2">
                  <div>
                    <p class="text-h6 font-weight-medium">{{ pick.name }}</p>
                    <p class="text-body-2 text-medium-emphasis">{{ pick.house }}</p>
                  </div>
                  <v-chip
                    size="small"
                    :color="pick.status === 'confirmed' ? 'success' : 'warning'"
                    variant="tonal"
                    class="ml-4 mt-1"
                  >
                    {{ pick.status }}
                  </v-chip>
                </div>
                <p class="text-body-2 mt-2" style="max-width: 680px">{{ pick.rationale }}</p>
              </v-card-text>
            </v-card>
          </div>
        </v-expansion-panel-text>
      </v-expansion-panel>
    </v-expansion-panels>
  </div>
</template>

<script setup lang="ts">
import { useRuns } from '~/composables/useRuns'

const { runs, loading, triggering, error, fetchRuns, triggerRun, stopPolling } = useRuns()
const triggerError = ref<string | null>(null)
const triggered = ref<string | null>(null)

onMounted(fetchRuns)

function statusColor(status: string) {
  return { pending: 'warning', running: 'info', done: 'success', failed: 'error' }[status] ?? 'default'
}

async function trigger() {
  triggerError.value = null
  triggered.value = null
  try {
    const { run_id } = await triggerRun()
    triggered.value = `Run started (ID ${run_id}). It will appear in the list below once complete.`
    stopPolling()
    await fetchRuns()
  } catch (err: any) {
    triggerError.value = err?.data?.detail ?? 'Failed to start run.'
  }
}
</script>
