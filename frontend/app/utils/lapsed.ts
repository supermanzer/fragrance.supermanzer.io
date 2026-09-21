export const READ_ONLY_WRITE_NOTICE =
  "That change wasn't saved. Your account is read-only right now."

export function formatExpiryDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
}

// "October 14"; the year is appended only when it differs from the current year.
export function formatNextRunDate(dateStr: string): string {
  const date = new Date(dateStr)
  const sameYear = date.getFullYear() === new Date().getFullYear()
  return date.toLocaleDateString('en-US', {
    month: 'long',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  })
}
