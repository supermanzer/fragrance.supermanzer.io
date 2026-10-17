// Validates a post-login `?redirect=` value. vue-router has already percent-decoded the query
// once by the time a page reads it, so the value is checked as given and, defensively, once
// more after a further decode (catches `%2f%2fevil.com` double-encoded forms). Only a
// same-origin absolute path is accepted: exactly one leading slash, no backslash, no control
// characters or whitespace (browsers strip tab/CR/LF from URLs, turning `/<TAB>/evil.com`
// into `//evil.com`). Anything else returns the fallback.
const CONTROL_OR_SPACE = /[\u0000- \u007f-\u009f]/

function isSafePath(value: string): boolean {
  if (!value.startsWith('/') || value.startsWith('//')) return false
  if (value.includes('\\')) return false
  if (CONTROL_OR_SPACE.test(value)) return false
  return true
}

export function safeRedirectPath(raw: unknown, fallback: string): string {
  if (typeof raw !== 'string' || !isSafePath(raw)) return fallback
  let decoded: string
  try {
    decoded = decodeURIComponent(raw)
  } catch {
    return fallback
  }
  return isSafePath(decoded) ? raw : fallback
}
