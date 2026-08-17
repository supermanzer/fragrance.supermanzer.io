<template>
  <div class="pt-8">
    <div class="d-flex align-start justify-space-between mb-8">
      <div class="d-flex flex-column">
        <h1 class="text-h3 mb-1">Runs</h1>
        <p class="text-body-2 text-medium-emphasis mb-0">Monthly AI-curated fragrance picks</p>
      </div>

      <!-- Wrap the disabled button in a span so the tooltip's pointer events
           still fire when the button itself suppresses them via :disabled. -->
      <VTooltip
        location="bottom"
        text="A run is already in progress"
        :disabled="!hasActiveRun"
      >
        <template #activator="{ props: tooltipProps }">
          <span v-bind="tooltipProps" class="d-inline-block">
            <VBtn
              color="primary"
              variant="flat"
              rounded="lg"
              :disabled="hasActiveRun"
              :loading="triggering"
              @click="trigger"
            >
              Trigger Run
            </VBtn>
          </span>
        </template>
      </VTooltip>
    </div>

    <VFadeTransition mode="out-in">
      <VSkeletonLoader
        v-if="loading"
        key="loading"
        type="list-item-two-line@4"
      />

      <VAlert
        v-else-if="error"
        key="error"
        type="error"
        variant="tonal"
        rounded="lg"
        class="mb-6"
      >
        {{ error }}
        <template #append>
          <VBtn
            variant="text"
            color="error"
            @click="fetchRuns()"
          >
            Retry
          </VBtn>
        </template>
      </VAlert>

      <div
        v-else-if="!runs.length"
        key="empty"
        class="text-center pa-8"
      >
        <VIcon
          icon="mdi-flask-outline"
          size="64"
          class="text-medium-emphasis mb-4"
        />
        <p class="text-body-1 text-medium-emphasis">No runs yet.</p>
        <p class="text-body-2 text-medium-emphasis mt-1">
          Trigger a run to generate your first five fragrance picks, curated from your collection.
        </p>
        <VBtn
          color="primary"
          variant="flat"
          rounded="lg"
          class="mt-6"
          @click="trigger"
        >
          Trigger your first run
        </VBtn>
      </div>

      <VExpansionPanels
        v-else
        key="populated"
        variant="accordion"
        multiple
        v-model="openPanels"
      >
        <VExpansionPanel
          v-for="run in runs"
          :key="run.id"
        >
          <VExpansionPanelTitle>
            <div class="d-flex align-center w-100 mr-4">
              <span class="text-body-1">{{ formatDate(run.triggered_at) }}</span>

              <template v-if="run.status === 'failed'">
                <VChip
                  size="small"
                  variant="tonal"
                  color="error"
                  class="ml-4"
                >
                  Failed
                </VChip>
              </template>
              <template v-else-if="run.status === 'pending'">
                <VChip
                  size="small"
                  variant="tonal"
                  color="warning"
                  class="ml-4"
                >
                  Pending
                </VChip>
              </template>
              <template v-else-if="run.status === 'running'">
                <VChip
                  size="small"
                  variant="tonal"
                  color="info"
                  class="ml-4"
                >
                  Running
                </VChip>
              </template>
              <template v-else-if="run.email_status === 'pending'">
                <VChip
                  size="small"
                  variant="tonal"
                  color="warning"
                  class="ml-4"
                >
                  Sending&hellip;
                </VChip>
                <VIcon
                  icon="mdi-email-sync-outline"
                  size="18"
                  class="text-medium-emphasis ml-2"
                />
              </template>
              <template v-else-if="run.email_status === 'failed'">
                <VChip
                  size="small"
                  variant="tonal"
                  color="error"
                  class="ml-4"
                >
                  Email failed
                </VChip>
              </template>
              <template v-else-if="run.email_status === 'sent'">
                <VIcon
                  icon="mdi-email-check-outline"
                  size="18"
                  class="text-medium-emphasis ml-2"
                />
              </template>
              <template v-else>
                <!-- done, email_status === null -->
                <VIcon
                  icon="mdi-email-off-outline"
                  size="18"
                  class="text-medium-emphasis ml-2"
                />
              </template>
            </div>
          </VExpansionPanelTitle>

          <VExpansionPanelText>
            <!-- In progress -->
            <template v-if="run.status === 'pending' || run.status === 'running'">
              <VProgressLinear
                indeterminate
                color="primary"
                :height="2"
                rounded
                class="mb-4"
              />
              <p class="text-body-2 text-medium-emphasis">
                Curating your picks — this takes about 2 minutes.
              </p>
            </template>

            <!-- Pipeline failed -->
            <template v-else-if="run.status === 'failed'">
              <VAlert
                type="error"
                variant="tonal"
                rounded="lg"
                density="compact"
              >
                {{ run.error_message }}
              </VAlert>
            </template>

            <!-- Done (all done sub-states share intro + picks; email alert is conditional) -->
            <template v-else>
              <VAlert
                v-if="run.email_status === 'failed' || run.email_status === null"
                type="warning"
                variant="tonal"
                rounded="lg"
                density="compact"
                class="mb-4"
              >
                {{ run.email_status === 'failed' ? 'Email delivery failed.' : 'Email not yet sent.' }}
                <template #append>
                  <VBtn
                    color="warning"
                    variant="flat"
                    size="small"
                    rounded="lg"
                    :loading="resending.has(run.id)"
                    @click="handleResend(run.id)"
                  >
                    Resend
                  </VBtn>
                </template>
              </VAlert>

              <p
                v-if="run.intro"
                class="text-body-2 font-italic mb-6"
                style="max-width: 640px"
              >
                {{ run.intro }}
              </p>

              <p
                v-if="run.picks.length"
                class="text-caption text-medium-emphasis mb-4"
              >
                {{ run.picks.length }} picks this month
              </p>

              <div class="d-flex flex-column">
                <template
                  v-for="(pick, index) in run.picks"
                  :key="pick.id"
                >
                  <VDivider v-if="index > 0" class="my-2" />
                  <div
                    class="pl-4 py-3"
                    style="border-left: 3px solid rgb(var(--v-theme-secondary))"
                  >
                    <p class="text-h6 font-weight-medium mb-0">{{ pick.name }}</p>
                    <p class="text-body-2 text-medium-emphasis mt-1">{{ pick.house }}</p>
                    <div class="d-flex ga-2 mt-2">
                      <VBtn
                        size="small"
                        variant="text"
                        prepend-icon="mdi-text"
                        @click="toggleRationale(pick.id)"
                      >
                        {{ openRationales.has(pick.id) ? 'Hide rationale' : 'Read rationale' }}
                      </VBtn>
                      <VBtn
                        v-if="pick.search_source_url"
                        size="small"
                        variant="text"
                        prepend-icon="mdi-open-in-new"
                        :href="pick.search_source_url"
                        target="_blank"
                        rel="noopener"
                      >
                        Source
                      </VBtn>
                    </div>
                    <VExpandTransition>
                      <p
                        v-if="openRationales.has(pick.id)"
                        class="text-body-2 mt-3"
                        style="max-width: 640px"
                      >
                        {{ pick.rationale }}
                      </p>
                    </VExpandTransition>

                    <VFadeTransition mode="out-in">
                      <VChip
                        v-if="pick.rating"
                        key="rated"
                        size="small"
                        variant="tonal"
                        :color="statusColor(pick.rating)"
                        :prepend-icon="ratingIcon(pick.rating)"
                        class="mt-3"
                      >
                        Marked as {{ ratingLabel(pick.rating) }}
                      </VChip>
                      <div
                        v-else
                        key="unrated"
                        class="d-flex ga-2 flex-wrap mt-3"
                      >
                        <VBtn
                          size="small"
                          variant="text"
                          prepend-icon="mdi-cash"
                          :loading="ratingInFlight.get(pick.id) === 'own'"
                          :disabled="ratingInFlight.has(pick.id) && ratingInFlight.get(pick.id) !== 'own'"
                          @click="handleRate(pick, 'own')"
                        >
                          Bought it
                        </VBtn>
                        <VBtn
                          size="small"
                          variant="text"
                          prepend-icon="mdi-thumb-up"
                          :loading="ratingInFlight.get(pick.id) === 'like'"
                          :disabled="ratingInFlight.has(pick.id) && ratingInFlight.get(pick.id) !== 'like'"
                          @click="handleRate(pick, 'like')"
                        >
                          Liked it
                        </VBtn>
                        <VBtn
                          size="small"
                          variant="text"
                          prepend-icon="mdi-thumb-down"
                          :loading="ratingInFlight.get(pick.id) === 'dislike'"
                          :disabled="ratingInFlight.has(pick.id) && ratingInFlight.get(pick.id) !== 'dislike'"
                          @click="handleRate(pick, 'dislike')"
                        >
                          Didn't like it
                        </VBtn>
                      </div>
                    </VFadeTransition>
                  </div>
                </template>
              </div>
            </template>
          </VExpansionPanelText>
        </VExpansionPanel>
      </VExpansionPanels>
    </VFadeTransition>

    <!-- Snackbar for transient feedback. One-way :model-value avoids Vuetify
         fighting the ref on timeout: the update:model-value handler nulls the
         ref so `:model-value` settles to false and the snackbar closes cleanly. -->
    <VSnackbar
      :model-value="!!snackbar"
      :color="snackbar?.color"
      :timeout="4000"
      variant="tonal"
      location="bottom"
      @update:model-value="(v) => { if (!v) snackbar = null }"
    >
      {{ snackbar?.message }}
    </VSnackbar>
  </div>
</template>

<script setup lang="ts">
import { useRuns, type Recommendation } from '~/composables/useRuns'
import { ratingLabel, ratingIcon, statusColor, type RatingAction } from '~/utils/rating'

const {
  runs,
  loading,
  triggering,
  error,
  hasActiveRun,
  resending,
  ratingInFlight,
  fetchRuns,
  triggerRun,
  resendEmail,
  rateRecommendation,
} = useRuns()

const openPanels = ref<number[]>([0])
const openRationales = ref<Set<number>>(new Set())
const snackbar = ref<{ message: string; color: 'success' | 'error' | 'info' } | null>(null)

onMounted(fetchRuns)

function formatDate(dateString: string): string {
  return new Date(dateString).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
}

function toggleRationale(id: number): void {
  if (openRationales.value.has(id)) {
    openRationales.value.delete(id)
  } else {
    openRationales.value.add(id)
  }
}

async function trigger(): Promise<void> {
  try {
    await triggerRun()
    snackbar.value = { message: 'Run triggered', color: 'success' }
    await fetchRuns()
  } catch {
    snackbar.value = { message: 'Failed to start run.', color: 'error' }
  }
}

async function handleResend(runId: number): Promise<void> {
  try {
    await resendEmail(runId)
    snackbar.value = { message: 'Email resend queued', color: 'success' }
  } catch {
    snackbar.value = { message: 'Failed to resend email', color: 'error' }
  }
}

async function handleRate(pick: Recommendation, action: RatingAction): Promise<void> {
  try {
    const result = await rateRecommendation(pick.id, action)
    if (result.outcome === 'already_rated') {
      snackbar.value = { message: 'That pick was already rated.', color: 'info' }
    } else {
      snackbar.value = {
        message: `${pick.name} added to your collection as ${ratingLabel(result.action)}.`,
        color: 'success',
      }
    }
  } catch {
    snackbar.value = { message: "Couldn't save your rating. Please try again.", color: 'error' }
  }
}
</script>
