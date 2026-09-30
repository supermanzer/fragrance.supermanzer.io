import type { RecommendationRun } from '~/composables/useRuns'

export type RunNoticeKind = 'none' | 'running-again' | 'retrying' | 'exhausted' | 'stopped'

export const SUPPORT_EMAIL = 'support@supermanzer.io'

// "shortly" when unknown or already due, "tomorrow" for the next local calendar day,
// otherwise "on October 3". Read as: "We'll try again {when}."
export function retryWhen(iso: string | null): string {
  if (!iso) return 'shortly'
  const at = new Date(iso)
  const now = new Date()
  if (at.getTime() <= now.getTime()) return 'shortly'
  const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  const isTomorrow =
    at.getFullYear() === tomorrow.getFullYear()
    && at.getMonth() === tomorrow.getMonth()
    && at.getDate() === tomorrow.getDate()
  return isTomorrow ? 'tomorrow' : `on ${formatNextRunDate(iso)}`
}

// Greatest triggered_at wins; array order is not trusted.
export function newestRun(runs: readonly RecommendationRun[]): RecommendationRun | null {
  let newest: RecommendationRun | null = null
  for (const run of runs) {
    if (!newest || new Date(run.triggered_at) > new Date(newest.triggered_at)) newest = run
  }
  return newest
}

// Mirrors backend RETRY_OVERDUE_GRACE: a scheduled retry overdue by more than this stops blocking.
const RETRY_OVERDUE_GRACE_MS = 2 * 60 * 60 * 1000

export function isRetryBlocking(run: RecommendationRun): boolean {
  if (run.status !== 'failed' || run.retry?.state !== 'scheduled') return false
  const next = run.retry.next_retry_at
  if (!next) return true
  return new Date(next).getTime() >= Date.now() - RETRY_OVERDUE_GRACE_MS
}

// A run that is in flight, or a failed run whose automatic retry is queued and not overdue.
export function isRunActive(run: RecommendationRun): boolean {
  return run.status === 'pending' || run.status === 'running' || isRetryBlocking(run)
}

export function isRunInFlight(run: RecommendationRun | null): boolean {
  return run?.status === 'pending' || run?.status === 'running'
}

export function runNotice(args: { isLapsed: boolean; newest: RecommendationRun | null }): RunNoticeKind {
  const { isLapsed, newest } = args
  if (!newest) return 'none'
  if (newest.status === 'failed') {
    if (isLapsed) return 'stopped'
    switch (newest.retry?.state) {
      case 'scheduled': return 'retrying'
      case 'exhausted': return 'exhausted'
      case 'stopped': return 'stopped'
      default: return 'none'
    }
  }
  if (isRunInFlight(newest) && newest.attempt > 1) return 'running-again'
  return 'none'
}
