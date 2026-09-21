<template>
  <v-app-bar elevation="0" border="b">
    <v-app-bar-title>Fragrances</v-app-bar-title>

    <v-btn :to="{ path: '/fragrance' }" variant="text" active-color="primary">Collection</v-btn>
    <v-btn :to="{ path: '/runs' }" variant="text" active-color="primary">Runs</v-btn>
    <v-btn :to="{ path: '/profile' }" variant="text" active-color="primary">Profile</v-btn>
    <v-btn :to="{ path: '/settings' }" variant="text" active-color="primary">Settings</v-btn>
    <v-btn
      v-if="known && !isExempt"
      :to="{ path: '/billing' }"
      variant="text"
      active-color="primary"
      class="d-none d-sm-flex"
    >
      Billing
    </v-btn>
    <v-btn :to="{ path: '/about' }" variant="text" active-color="primary">About</v-btn>

    <template #append>
      <v-btn v-tooltip:start="'Log out'" icon @click="logout">
        <v-icon>mdi-logout</v-icon>
      </v-btn>
    </template>
  </v-app-bar>

  <v-main>
    <v-container class="pt-6 pb-0">
      <LapsedBanner />
    </v-container>
    <v-container class="pt-8">
      <slot />
    </v-container>
  </v-main>

  <v-divider class="mb-0" />
  <v-footer color="background" class="mt-8 pt-4 pb-4">
    <v-container class="py-0">
      <div class="d-flex justify-center justify-md-end ga-2 ga-md-4">
        <v-btn
          variant="text"
          size="small"
          to="/terms"
          class="text-decoration-underline text-medium-emphasis"
        >
          Terms
        </v-btn>
        <v-btn
          variant="text"
          size="small"
          to="/privacy"
          class="text-decoration-underline text-medium-emphasis"
        >
          Privacy
        </v-btn>
      </div>
    </v-container>
  </v-footer>
</template>

<script setup lang="ts">
import { clearTokens } from '~/composables/useApi'

const route = useRoute()
const subscriptionStale = useState<number>('subscription-stale', () => 0)
const { fetchSubscription, refreshSubscription, known, isExempt } = useSubscription()
const resetSessionState = useSessionReset()

onMounted(fetchSubscription)

// After Stripe redirects back, the layout mounts on /billing/success where the first
// fetch may pre-date fulfilment; refetch when leaving /billing* so the banner is truthful.
watch(
  () => route.path,
  (next, prev) => {
    if (prev?.startsWith('/billing') && !next.startsWith('/billing')) refreshSubscription()
  },
)

watch(subscriptionStale, () => {
  refreshSubscription()
})

async function logout() {
  clearTokens()
  resetSessionState()
  await navigateTo('/auth/login')
}
</script>
