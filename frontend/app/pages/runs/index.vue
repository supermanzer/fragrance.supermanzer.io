<template>
  <div>
    <div class="d-flex align-center mb-6">
      <h1 class="text-h4">Recommendation Runs</h1>
      <v-spacer />
      <v-btn color="primary" prepend-icon="mdi-play" :loading="triggering" @click="trigger">
        Run Now
      </v-btn>
    </div>

    <v-alert v-if="triggerError" type="error" class="mb-4">{{ triggerError }}</v-alert>
    <v-alert v-if="triggered" type="success" class="mb-4">
      Run started (ID {{ lastRunId }}). It will appear in the list below once complete.
    </v-alert>
    <v-alert v-if="error" type="error" class="mb-4">{{ error }}</v-alert>

    <v-skeleton-loader v-if="loading" type="list-item-three-line@3" />

    <v-alert v-else-if="!runs.length" type="info" variant="tonal">
      No runs yet. Click "Run Now" to generate your first recommendations.
    </v-alert>

    <v-expansion-panels v-else>
      <v-expansion-panel v-for="run in runs" :key="run.id">
        <v-expansion-panel-title>
          <div class="d-flex align-center gap-4 w-100">
            <span>{{ new Date(run.triggered_at).toLocaleString() }}</span>
            <v-chip :color="statusColor(run.status)" size="small" class="ml-4">{{ run.status }}</v-chip>
            <v-spacer />
            <span class="text-caption text-medium-emphasis mr-4">{{ run.picks.length }} pick(s)</span>
          </div>
        </v-expansion-panel-title>

        <v-expansion-panel-text>
          <p v-if="run.intro" class="mb-4 font-italic">{{ run.intro }}</p>

          <v-alert v-if="run.error_message" type="error" variant="tonal" class="mb-4">
            {{ run.error_message }}
          </v-alert>

          <v-list v-if="run.picks.length">
            <v-list-item
              v-for="pick in run.picks"
              :key="pick.id"
              :title="pick.name"
              :subtitle="pick.house"
            >
              <template #append>
                <v-chip size="small" :color="pick.status === 'confirmed' ? 'success' : 'warning'">
                  {{ pick.status }}
                </v-chip>
              </template>
              <template #default>
                <v-list-item-title>{{ pick.name }}</v-list-item-title>
                <v-list-item-subtitle>{{ pick.house }}</v-list-item-subtitle>
                <p class="text-body-2 mt-1">{{ pick.rationale }}</p>
              </template>
            </v-list-item>
          </v-list>
        </v-expansion-panel-text>
      </v-expansion-panel>
    </v-expansion-panels>
  </div>
</template>

<script setup lang="ts">
import { useRuns } from '~/composables/useRuns'

const { runs, loading, triggering, error, fetchRuns, triggerRun } = useRuns()
const triggerError = ref<string | null>(null)
const triggered = ref(false)
const lastRunId = ref<number | null>(null)

onMounted(fetchRuns)

function statusColor(status: string) {
  return { pending: 'warning', running: 'info', done: 'success', failed: 'error' }[status] ?? 'default'
}

async function trigger() {
  triggerError.value = null
  triggered.value = false
  try {
    const { run_id } = await triggerRun()
    lastRunId.value = run_id
    triggered.value = true
    await fetchRuns()
  } catch (err: any) {
    triggerError.value = err?.data?.detail ?? 'Failed to start run.'
  }
}
</script>
