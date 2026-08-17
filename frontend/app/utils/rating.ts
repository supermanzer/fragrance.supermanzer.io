export type RatingAction = 'own' | 'like' | 'dislike'

const COLORS: Record<RatingAction, string> = {
  own: 'success',
  like: 'info',
  dislike: 'error',
}

const LABELS: Record<RatingAction, string> = {
  own: 'Own',
  like: 'Liked',
  dislike: 'Not for Me',
}

const ICONS: Record<RatingAction, string> = {
  own: 'mdi-cash',
  like: 'mdi-thumb-up',
  dislike: 'mdi-thumb-down',
}

// Shared own/like/dislike -> success/info/error mapping. Originally local to
// fragrance/index.vue; extracted here so runs/index.vue and confirm.vue reuse
// the same mapping instead of inventing a second one for the same three values.
export function statusColor(status: string): string {
  return COLORS[status as RatingAction] ?? 'default'
}

export function ratingLabel(action: RatingAction): string {
  return LABELS[action]
}

export function ratingIcon(action: RatingAction): string {
  return ICONS[action]
}
