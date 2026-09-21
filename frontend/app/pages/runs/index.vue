<template>
  <div class="pt-8">
    <div class="d-flex flex-column flex-sm-row align-sm-start justify-space-between ga-4 mb-8">
      <div class="d-flex flex-column">
        <h1 class="text-h3 mb-1">Runs</h1>
        <p class="text-body-2 text-medium-emphasis mb-0">Monthly AI-curated fragrance picks</p>
      </div>

      <div class="d-flex flex-column align-start align-sm-end">
        <LapsedGate
          :locked="!available"
          label="Trigger Run"
          :reason="triggerReason"
        >
          <VBtn
            color="primary"
            variant="flat"
            rounded="lg"
            :disabled="!available"
            :loading="triggering"
            @click="trigger"
          >
            Trigger Run
          </VBtn>
        </LapsedGate>

        <div class="mt-2 text-sm-right" style="max-width: 280px">
          <VFadeTransition mode="out-in">
            <VSkeletonLoader
              v-if="quotaLoading"
              key="quota-skeleton"
              type="text"
              width="180"
            />
            <div v-else-if="quotaCaption" :key="quotaCaption.key">
              <p class="text-caption text-medium-emphasis mb-0">{{ quotaCaption.text }}</p>
              <p
                v-if="quotaCaption.key === 'quota-available' && lastRunFailed"
                class="text-caption text-medium-emphasis mb-0 mt-1"
              >
                Your last run didn't finish, so it wasn't counted.
              </p>
            </div>
          </VFadeTransition>
        </div>
      </div>
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
        <template v-if="isLapsed">
          <p class="text-body-2 text-medium-emphasis mt-1">
            Recommendations arrive by email once you have an active subscription.
          </p>
          <VBtn
            color="primary"
            variant="flat"
            rounded="lg"
            class="mt-6"
            to="/billing"
          >
            View plans
          </VBtn>
        </template>
        <template v-else>
        <p class="text-body-2 text-medium-emphasis mt-1">
          Trigger a run to generate your first five fragrance picks, curated from your collection.
        </p>
        <LapsedGate
          :locked="!available"
          label="Trigger your first run"
          :reason="triggerReason"
        >
          <VBtn
            color="primary"
            variant="flat"
            rounded="lg"
            class="mt-6"
            :disabled="!available"
            :loading="triggering"
            @click="trigger"
          >
            Trigger your first run
          </VBtn>
        </LapsedGate>
        </template>
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
                  <LapsedGate
                    :locked="isLapsed"
                    label="Resend"
                    reason="Resending needs an active subscription."
                    location="top"
                  >
                    <VBtn
                      color="warning"
                      variant="flat"
                      size="small"
                      rounded="lg"
                      :loading="resending.has(run.id)"
                      :disabled="isLapsed"
                      @click="handleResend(run.id)"
                    >
                      Resend
                    </VBtn>
                  </LapsedGate>
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
import { isSubscriptionRequired } from '~/composables/useApi'
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
const {
  subscription,
  loading: subscriptionLoading,
  isLapsed,
  quota,
  unlimited,
  fetchSubscription,
  refreshSubscription,
} = useSubscription()

// Bridge holding next_eligible_at from a 429 quota_exceeded until the refetch lands.
const blockedUntil = ref<string | null>(null)
watch(subscriptionLoading, (isLoading) => {
  if (!isLoading) blockedUntil.value = null
})

const inProgress = computed(
  () => !isLapsed.value && (hasActiveRun.value || (unlimited.value && quota.value?.can_trigger === false)),
)
const quotaUsed = computed(
  () =>
    !isLapsed.value
    && !inProgress.value
    && !unlimited.value
    && (quota.value?.can_trigger === false || blockedUntil.value !== null),
)
const available = computed(() => !isLapsed.value && !inProgress.value && !quotaUsed.value)

// A 429 is fresher than a cached page, so blockedUntil wins only when it is later.
const nextEligibleAt = computed<string | null>(() => {
  const fromQuota = quota.value?.next_eligible_at ?? null
  const fromBridge = blockedUntil.value
  if (fromQuota && fromBridge) return new Date(fromBridge) > new Date(fromQuota) ? fromBridge : fromQuota
  return fromQuota ?? fromBridge
})
const nextRunDate = computed(() => (nextEligibleAt.value ? formatNextRunDate(nextEligibleAt.value) : null))

const triggerReason = computed(() => {
  if (isLapsed.value) return 'Running needs an active subscription.'
  if (inProgress.value) return 'A run is already in progress'
  return nextRunDate.value
    ? `Your next run is available on ${nextRunDate.value}.`
    : 'Your next run will be available when this period ends.'
})

const quotaLoading = computed(() => subscriptionLoading.value && !subscription.value)

const lastRunFailed = computed(() => {
  let newest: (typeof runs.value)[number] | null = null
  for (const run of runs.value) {
    if (!newest || new Date(run.triggered_at) > new Date(newest.triggered_at)) newest = run
  }
  return newest?.status === 'failed'
})

const quotaCaption = computed<{ key: string; text: string } | null>(() => {
  const q = quota.value
  // No caption for lapsed, exempt (unlimited), or unknown quota; also never
  // interpolate "{used} of {allowed}" while runs_allowed is null.
  if (isLapsed.value || unlimited.value || q === null || q.runs_allowed === null) return null
  const noun = q.runs_allowed === 1 ? 'run' : 'runs'
  const usage = `${q.runs_used} of ${q.runs_allowed} ${noun} used this period`
  if (quotaUsed.value) {
    if (q.can_trigger === false && q.runs_used < q.runs_allowed) {
      return {
        key: 'quota-blocked',
        text: nextRunDate.value
          ? `Two recent runs didn't finish. Your next run is available on ${nextRunDate.value}.`
          : "Two recent runs didn't finish. Runs will be available again soon.",
      }
    }
    return {
      key: 'quota-used',
      text: nextRunDate.value
        ? `${usage}. Your next run is available on ${nextRunDate.value}.`
        : `${usage}. Your next run will be available when this period ends.`,
    }
  }
  return { key: available.value ? 'quota-available' : 'quota-in-progress', text: usage }
})

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
  if (!available.value) return
  try {
    await triggerRun()
    snackbar.value = { message: 'Run triggered', color: 'success' }
    await Promise.all([fetchRuns(), refreshSubscription()])
  } catch (err: unknown) {
    const e = err as { status?: number; data?: { code?: string; next_eligible_at?: string | null } }
    if (e?.status === 429 && e.data?.code === 'quota_exceeded') {
      blockedUntil.value = e.data.next_eligible_at ?? null
      const date = blockedUntil.value ? formatNextRunDate(blockedUntil.value) : null
      snackbar.value = {
        message: date ? `Your next run is available on ${date}.` : "You've already used this period's run.",
        color: 'info',
      }
      await refreshSubscription()
    } else if (e?.status === 429 && e.data?.code === 'run_in_progress') {
      snackbar.value = { message: 'A run is already in progress.', color: 'info' }
      await fetchRuns(true)
    } else if (isSubscriptionRequired(err)) {
      snackbar.value = {
        message: "Your account is read-only right now, so a run can't be started.",
        color: 'info',
      }
    } else {
      snackbar.value = { message: 'Failed to start run.', color: 'error' }
    }
  }
}

async function handleResend(runId: number): Promise<void> {
  if (isLapsed.value) return
  try {
    await resendEmail(runId)
    snackbar.value = { message: 'Email resend queued', color: 'success' }
  } catch (err: unknown) {
    snackbar.value = isSubscriptionRequired(err)
      ? { message: "Your account is read-only right now, so the email can't be resent.", color: 'info' }
      : { message: 'Failed to resend email', color: 'error' }
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
