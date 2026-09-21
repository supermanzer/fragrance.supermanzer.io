export interface PlanPriceInput {
  amount_cents: number
  currency: string
  billing_interval: string
}

export function formatPlanPrice(plan: PlanPriceInput): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: plan.currency }).format(
    plan.amount_cents / 100,
  )
}

// "month" / "year" for "$4.00 / month" style lines.
export function intervalPhrase(plan: PlanPriceInput): string {
  return plan.billing_interval === 'year' ? 'year' : 'month'
}

const STRIPE_PORTAL_HOST = 'billing.stripe.com'
const STRIPE_CHECKOUT_HOST = 'checkout.stripe.com'

function isHttpsHost(raw: unknown, host: string): raw is string {
  if (typeof raw !== 'string') return false
  try {
    const parsed = new URL(raw)
    return parsed.protocol === 'https:' && parsed.host === host
  } catch {
    return false
  }
}

export function isValidPortalUrl(raw: unknown): raw is string {
  return isHttpsHost(raw, STRIPE_PORTAL_HOST)
}

export function isValidCheckoutUrl(raw: unknown): raw is string {
  return isHttpsHost(raw, STRIPE_CHECKOUT_HOST)
}
