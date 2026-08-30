<template>
  <div>
    <!-- ─── HERO ─────────────────────────────────────────────────────────── -->
    <section class="mb-16">
      <VRow align="center">
        <VCol
          cols="12"
          md="6"
          order="2"
          order-md="1"
          class="pr-md-10"
        >
          <p
            class="text-overline text-medium-emphasis mb-3"
            style="letter-spacing: 0.12em"
          >
            For one collection. Yours.
          </p>
          <h1
            class="text-h3 mb-6"
            style="font-weight: 400; line-height: 1.2"
          >
            Recommendations built from what you already own.
          </h1>
          <p
            class="text-body-1 mb-6"
            style="max-width: 520px; line-height: 1.8"
          >
            Sign in and tell it what's in your collection: what you own, what you like, what missed the
            mark. Once a month it searches, verifies, and sends five fragrances chosen to push your
            taste further in the direction it's already heading.
          </p>

          <div class="d-flex flex-column flex-sm-row ga-3 ga-sm-4">
            <VBtn
              color="primary"
              variant="flat"
              rounded="lg"
              size="large"
              :to="authStatus === 'authenticated' ? '/fragrance' : '/auth/login'"
              class="w-100 w-sm-auto"
            >
              Get started
            </VBtn>
            <VBtn
              variant="outlined"
              rounded="lg"
              size="large"
              :to="authStatus === 'authenticated' ? '/fragrance' : '/auth/login'"
              class="w-100 w-sm-auto"
            >
              Sign in
            </VBtn>
          </div>
        </VCol>

        <VCol
          cols="12"
          md="6"
          order="1"
          order-md="2"
          class="mb-8 mb-md-0"
        >
          <VImg
            class="d-md-none"
            src="https://images.pexels.com/photos/30970929/pexels-photo-30970929.jpeg?auto=compress&cs=tinysrgb&w=640"
            :aspect-ratio="1.1"
            rounded="lg"
            cover
            referrerpolicy="no-referrer"
            alt="A minimalist glass perfume bottle centered against a neutral background, soft natural light casting a gentle shadow"
          >
            <template #placeholder>
              <VSkeletonLoader
                type="image"
                height="100%"
              />
            </template>
          </VImg>
          <VImg
            class="d-none d-md-block"
            src="https://images.pexels.com/photos/30970929/pexels-photo-30970929.jpeg?auto=compress&cs=tinysrgb&w=960"
            :aspect-ratio="0.85"
            rounded="lg"
            cover
            referrerpolicy="no-referrer"
            alt="A minimalist glass perfume bottle centered against a neutral background, soft natural light casting a gentle shadow"
          >
            <template #placeholder>
              <VSkeletonLoader
                type="image"
                height="100%"
              />
            </template>
          </VImg>
        </VCol>
      </VRow>
    </section>

    <!-- ─── DIVIDER ───────────────────────────────────────────────────────── -->
    <VDivider class="mb-16" />

    <!-- ─── WHY THIS IS DIFFERENT ─────────────────────────────────────────── -->
    <section class="mb-16">
      <VRow>
        <VCol
          cols="12"
          md="4"
          class="pr-md-10 mb-8 mb-md-0"
        >
          <p
            class="text-overline text-medium-emphasis mb-3"
            style="letter-spacing: 0.12em"
          >
            Why this works
          </p>
          <h2
            class="text-h5 mb-4"
            style="font-weight: 400; line-height: 1.35"
          >
            One collection. One point of view.
          </h2>
          <p
            class="text-body-1 text-medium-emphasis"
            style="max-width: 460px; line-height: 1.8"
          >
            It stays that way for three reasons, each one running quietly in the background every month.
          </p>
        </VCol>

        <VCol cols="12" md="8">
          <VRow>
            <VCol
              v-for="(block, index) in benefitBlocks"
              :key="block.label"
              cols="12"
              sm="6"
              md="4"
              :class="index < benefitBlocks.length - 1 ? 'mb-8 mb-md-0' : 'mb-0'"
            >
              <VIcon
                :icon="block.icon"
                size="28"
                color="primary"
                class="mb-3"
              />
              <p
                class="text-overline text-medium-emphasis mb-2"
                style="letter-spacing: 0.1em"
              >
                {{ block.label }}
              </p>
              <h3 class="text-h6 font-weight-medium mb-2">{{ block.headline }}</h3>
              <p
                class="text-body-2 text-medium-emphasis"
                style="line-height: 1.7"
              >
                {{ block.body }}
              </p>
            </VCol>
          </VRow>
        </VCol>
      </VRow>
    </section>

    <!-- ─── DIVIDER ───────────────────────────────────────────────────────── -->
    <VDivider class="mb-16" />

    <!-- ─── CLOSING CTA ───────────────────────────────────────────────────── -->
    <section
      class="py-8 text-center"
      style="max-width: 640px; margin: 0 auto"
    >
      <p
        class="text-overline text-medium-emphasis mb-3"
        style="letter-spacing: 0.12em"
      >
        Ready when you are
      </p>
      <h2
        class="text-h5 mb-4"
        style="font-weight: 400; line-height: 1.35"
      >
        Start your own collection.
      </h2>
      <p
        class="text-body-1 mb-6"
        style="max-width: 480px; line-height: 1.8; margin: 0 auto"
      >
        Two minutes to sign in and start rating fragrances. Your first recommendation letter follows a
        month later.
      </p>

      <div class="d-flex flex-column flex-sm-row justify-sm-center ga-3 ga-sm-4">
        <VBtn
          color="primary"
          variant="flat"
          rounded="lg"
          size="large"
          :to="authStatus === 'authenticated' ? '/fragrance' : '/auth/login'"
          class="w-100 w-sm-auto"
        >
          Get started
        </VBtn>
        <VBtn
          variant="outlined"
          rounded="lg"
          size="large"
          :to="authStatus === 'authenticated' ? '/fragrance' : '/auth/login'"
          class="w-100 w-sm-auto"
        >
          Sign in
        </VBtn>
      </div>

      <p class="text-caption text-medium-emphasis mt-6">
        Want to see how it actually works first?
        <VBtn
          variant="text"
          size="small"
          to="/about"
          class="text-decoration-underline"
        >
          Read the full process →
        </VBtn>
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ layout: 'public', auth: false })

useSeoMeta({
  title: 'Fragrance recommendations, built from what you already own',
  description:
    "Tell it what you own, like, and what missed the mark. Once a month it searches, verifies, and emails five fragrances chosen to push your taste further in the direction it's already heading.",
  ogTitle: 'Fragrance recommendations, built from what you already own',
  ogDescription:
    'A personal fragrance recommendation letter, built entirely from your own collection.',
  ogImage:
    'https://images.pexels.com/photos/30970929/pexels-photo-30970929.jpeg?auto=compress&cs=tinysrgb&w=960',
  twitterCard: 'summary_large_image',
})

interface BenefitBlock {
  readonly icon: string
  readonly label: string
  readonly headline: string
  readonly body: string
}

const benefitBlocks: readonly BenefitBlock[] = [
  {
    icon: 'mdi-bottle-tonic-outline',
    label: 'Your collection',
    headline: "Every pick starts with what you've already rated.",
    body: "The system reads what you own, what you like, and what you've decided isn't for you, then builds a taste profile from exactly that. Nothing borrowed from anyone else's collection.",
  },
  {
    icon: 'mdi-check-decagram-outline',
    label: 'The process',
    headline: 'Verified before it ever reaches your inbox.',
    body: 'Five candidates are found, checked against everything recommended before, and confirmed real and currently available. Each one is new to you and still on the shelf.',
  },
  {
    icon: 'mdi-email-outline',
    label: 'The delivery',
    headline: 'One email a month, written around your taste.',
    body: 'A short letter arrives, once a month. Five fragrances, each with the reason it was picked for you.',
  },
]

const { authStatus, checkAuth } = useAuthStatus()

onMounted(() => {
  void checkAuth()
})
</script>
