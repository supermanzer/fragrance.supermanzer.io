// The one place the support address is read. Value comes from
// runtimeConfig.public.supportEmail (NUXT_PUBLIC_SUPPORT_EMAIL, default support@supermanzer.io).
// Call during setup, like any composable that touches the Nuxt context.
export function useSupportEmail(): string {
  return useRuntimeConfig().public.supportEmail as string
}

// The locked-account wording every static surface ends on. Matches the server's account_locked
// 409 `detail` exactly (address from settings); this is the static counterpart.
export function lockedRestartSentence(supportEmail: string): string {
  return `This account has been locked. Please reach out to ${supportEmail} if you wish to restart your subscription.`
}
