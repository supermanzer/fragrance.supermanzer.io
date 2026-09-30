<template>
  <div class="pt-8">
    <div class="d-flex flex-column flex-sm-row align-sm-start justify-space-between ga-4 mb-8">
      <div class="d-flex flex-column">
        <h1 class="text-h3 mb-1">Runs</h1>
        <p class="text-body-2 text-medium-emphasis mb-0">Monthly AI-curated fragrance picks</p>
      </div>

      <VFadeTransition mode="out-in">
        <VSkeletonLoader
          v-if="headerMode === 'unknown'"
          key="unknown"
          type="text"
          width="180"
        />

        <div
          v-else-if="headerMode === 'staff'"
          key="staff"
          class="d-flex flex-column align-start align-sm-end"
          style="max-width: 280px"
        >
          <LapsedGate
            :locked="!staffAvailable"
            label="Run now"
            :reason="triggerReason"
          >
            <VBtn
              color="primary"
              variant="flat"
              rounded="lg"
              prepend-icon="mdi-play-outline"
              :disabled="!staffAvailable"
              :loading="triggering"
              @click="trigger"
            >
              Run now
            </VBtn>
          </LapsedGate>
          <p class="text-caption text-medium-emphasis mb-0 mt-2 text-sm-right">
            Staff only. Starts a run outside the schedule.
          </p>
          <p
            v-if="retryBlocksStaff"
            class="text-caption text-medium-emphasis mb-0 mt-1 text-sm-right"
          >
            {{ retryBlockNote }}
          </p>
        </div>

        <div
          v-else-if="headerMode === 'first-picks'"
          key="first-picks"
          class="d-flex flex-column align-start align-sm-end"
          style="max-width: 280px"
        >
          <VBtn
            color="primary"
            variant="flat"
            rounded="lg"
            :loading="triggering"
            @click="trigger"
          >
            Get my first picks
          </VBtn>
        </div>

        <div
          v-else-if="headerLine"
          :key="headerMode"
          class="d-flex flex-column align-start align-sm-end"
          style="max-width: 280px"
        >
          <p class="text-body-2 text-medium-emphasis mb-0 text-sm-right">{{ headerLine }}</p>
          <VBtn
            variant="text"
            color="primary"
            rounded="lg"
            size="small"
            class="px-0 px-sm-3"
            to="/settings"
          >
            Change schedule
          </VBtn>
        </div>
      </VFadeTransition>
    </div>

    <RunStatusNotice
      v-if="!loading && !error && runs.length"
      :kind="noticeKind === 'none' ? null : noticeKind"
      :title="noticeTitle"
      :body="noticeBody"
      :show-support="noticeKind === 'exhausted'"
    />

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
        <template v-else-if="isStaff">
          <p class="text-body-2 text-medium-emphasis mt-1">
            Start a run to generate your first five fragrance picks, curated from your collection.
          </p>
          <LapsedGate
            :locked="!staffAvailable"
            label="Run now"
            :reason="triggerReason"
          >
            <VBtn
              color="primary"
              variant="flat"
              rounded="lg"
              class="mt-6"
              prepend-icon="mdi-play-outline"
              :disabled="!staffAvailable"
              :loading="triggering"
              @click="trigger"
            >
              Run now
            </VBtn>
          </LapsedGate>
          <p
            v-if="retryBlocksStaff"
            class="text-caption text-medium-emphasis mt-2 mb-0"
          >
            {{ retryBlockNote }}
          </p>
        </template>
        <template v-else-if="known">
          <p class="text-body-2 text-medium-emphasis mt-1">{{ emptyLine }}</p>
          <div class="d-flex flex-column align-center ga-2 mt-6">
            <VBtn
              v-if="firstRunAvailable"
              color="primary"
              variant="flat"
              rounded="lg"
              :loading="triggering"
              @click="trigger"
            >
              Get my first picks
            </VBtn>
            <VBtn
              variant="text"
              color="primary"
              rounded="lg"
              to="/settings"
            >
              Check your schedule
            </VBtn>
          </div>
        </template>
        <VSkeletonLoader
          v-else
          type="text"
          width="240"
          class="mx-auto mt-1"
        />
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
                  :color="failedChip(run).color"
                  class="ml-4"
                >
                  {{ failedChip(run).label }}
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
              <p
                v-if="run.attempt > 1"
                class="text-caption text-medium-emphasis mb-1"
              >
                Attempt {{ run.attempt }} of {{ run.max_attempts }}.
              </p>
              <p class="text-body-2 text-medium-emphasis">
                Curating your picks — this takes about 2 minutes.
              </p>
            </template>

            <!-- Pipeline failed -->
            <template v-else-if="run.status === 'failed'">
              <p class="text-body-2 mb-0">{{ failedSentence(run) }}</p>
              <VAlert
                v-if="isStaff && run.error_message"
                type="error"
                variant="tonal"
                density="compact"
                rounded="lg"
                class="mt-4"
              >
                <p class="text-subtitle-2 mb-1">Staff detail</p>
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
                v-if="run.attempt > 1"
                class="text-caption text-medium-emphasis mb-2"
              >
                Delivered on attempt {{ run.attempt }}.
              </p>

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
                        v-if="safeHttpUrl(pick.search_source_url)"
                        size="small"
                        variant="text"
                        prepend-icon="mdi-open-in-new"
                        :href="safeHttpUrl(pick.search_source_url) ?? undefined"
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
import { useRuns, type Recommendation, type RecommendationRun } from '~/composables/useRuns'
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
  isLapsed,
  isStaff,
  known,
  quota,
  refreshSubscription,
} = useSubscription()

const newest = computed(() => newestRun(runs.value))
const noticeKind = computed(() => runNotice({ isLapsed: isLapsed.value, newest: newest.value }))

// Any run pending/running, or a failed run whose retry is queued, blocks a manual start.
const anyActive = computed(() => runs.value.some(isRunActive))
const staffAvailable = computed(() => isStaff.value && !anyActive.value)
const firstRunAvailable = computed(
  () => known.value && !isStaff.value && !isLapsed.value && !anyActive.value && quota.value?.first_run_available === true,
)
const nextRunAt = computed(() => quota.value?.next_run_at ?? null)

const retryBlocksStaff = computed(
  () => isStaff.value && runs.value.some((r) => r.status === 'failed' && r.retry?.state === 'scheduled'),
)
const retryBlockNote = 'A retry is scheduled; Run now is unavailable until it finishes.'

function safeHttpUrl(url: string | null | undefined): string | null {
  if (!url) return null
  return /^https?:\/\//i.test(url.trim()) ? url.trim() : null
}

const triggerReason = computed(() => 'A run is already in progress')

type HeaderMode = 'unknown' | 'staff' | 'first-picks' | 'curating' | 'next-date' | 'monthly' | 'none'
const headerMode = computed<HeaderMode>(() => {
  if (!known.value) return 'unknown'
  if (isStaff.value) return 'staff'
  if (isRunInFlight(newest.value)) return 'curating'
  if (isLapsed.value) return 'none'
  if (firstRunAvailable.value) return 'first-picks'
  const n = newest.value
  if (n?.status === 'failed' && (n.retry?.state === 'scheduled' || n.retry?.state === 'exhausted')) return 'none'
  return nextRunAt.value ? 'next-date' : 'monthly'
})
const headerLine = computed(() => {
  switch (headerMode.value) {
    case 'curating': return 'Curating your picks now.'
    case 'next-date': return `Your next picks arrive on ${formatNextRunDate(nextRunAt.value!)}.`
    case 'monthly': return 'Your picks arrive by email on your monthly schedule.'
    default: return ''
  }
})

const emptyLine = computed(() =>
  nextRunAt.value
    ? `Your first five picks will arrive by email on ${formatNextRunDate(nextRunAt.value)}. They'll appear here too.`
    : "Your first five picks will arrive by email on your monthly schedule. They'll appear here too.",
)

const noticeTitle = computed(() => {
  switch (noticeKind.value) {
    case 'retrying': return "This month's run didn't finish. We're on it."
    case 'running-again': return 'Trying again now'
    case 'exhausted': return "We couldn't create this month's picks"
    case 'stopped': return "This month's run didn't finish"
    default: return ''
  }
})
const noticeBody = computed(() => {
  const run = newest.value
  if (!run) return ''
  const { attempt, max_attempts: max } = run
  switch (noticeKind.value) {
    case 'retrying': {
      const when = retryWhen(run.retry?.next_retry_at ?? null)
      return run.retry?.retries_remaining === 1
        ? `Attempt ${attempt} of ${max} ran into a problem on our side. We'll try once more ${when}, and that will be our last automatic try. There's nothing you need to do. We've emailed you a short note too.`
        : `Attempt ${attempt} of ${max} ran into a problem on our side. We'll try again ${when} and keep trying automatically, so there's nothing you need to do. We've emailed you a short note too.`
    }
    case 'running-again':
      return `Attempt ${attempt} of ${max} is running. It takes about 2 minutes, and your picks will arrive by email as soon as they're ready.`
    case 'exhausted':
      return `We tried ${max} times and weren't able to finish. We've been alerted and are looking into it. If you'd like to hear from us directly, write to ${SUPPORT_EMAIL}.`
    case 'stopped':
      return `We won't try again while your account is read-only. Once your subscription is active again, write to ${SUPPORT_EMAIL} and we'll look at it.`
    default:
      return ''
  }
})

function failedChip(run: RecommendationRun): { label: string; color: string } {
  return run.retry?.state === 'scheduled' && !isLapsed.value
    ? { label: 'Retrying', color: 'info' }
    : { label: "Didn't finish", color: 'error' }
}

function failedSentence(run: RecommendationRun): string {
  if (isLapsed.value) return "This run didn't finish."
  switch (run.retry?.state) {
    case 'scheduled':
      return `This run ran into a problem on our side. We'll try again ${retryWhen(run.retry.next_retry_at)}.`
    case 'exhausted':
      return `This run couldn't be completed after ${run.max_attempts} attempts. If you'd like to hear from us, write to ${SUPPORT_EMAIL}.`
    default:
      return "This run didn't finish."
  }
}

const openPanels = ref<number[]>([0])
const openRationales = ref<Set<number>>(new Set())
const snackbar = ref<{ message: string; color: 'success' | 'error' | 'info' } | null>(null)

onMounted(fetchRuns)

// Announce only the two transitions a customer can act on, and only when they happen
// while the page is open (the watch is not immediate).
watch(noticeKind, (kind, previous) => {
  if (kind === 'exhausted' && previous !== 'exhausted') {
    snackbar.value = {
      message: "We couldn't create this month's picks. Details are above the list.",
      color: 'info',
    }
  }
})
watch(
  () => [newest.value?.id, newest.value?.status] as const,
  ([id, status], [prevId, prevStatus]) => {
    if (id !== undefined && id === prevId && status === 'done' && prevStatus !== 'done') {
      snackbar.value = { message: 'Your picks are ready.', color: 'success' }
    }
  },
)

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
  if (triggering.value) return
  if (isStaff.value ? !staffAvailable.value : !firstRunAvailable.value) return
  try {
    await triggerRun()
    snackbar.value = { message: 'Run triggered', color: 'success' }
    await Promise.all([fetchRuns(), refreshSubscription()])
  } catch (err: unknown) {
    const e = err as { status?: number; data?: { code?: string; next_eligible_at?: string | null } }
    if (e?.status === 403 && e.data?.code === 'manual_runs_staff_only') {
      snackbar.value = { message: 'Your picks are delivered automatically each month.', color: 'info' }
      await refreshSubscription()
    } else if (e?.status === 429 && e.data?.code === 'quota_exceeded') {
      const date = e.data.next_eligible_at ? formatNextRunDate(e.data.next_eligible_at) : null
      snackbar.value = {
        message: date ? `Your next run is available on ${date}.` : "You've already used this period's run.",
        color: 'info',
      }
      await refreshSubscription()
    } else if (e?.status === 429 && e.data?.code === 'run_in_progress') {
      snackbar.value = { message: 'A run is already in progress.', color: 'info' }
      await Promise.all([fetchRuns(true), refreshSubscription()])
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
