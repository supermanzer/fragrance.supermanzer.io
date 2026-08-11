<template>
  <div class="pt-8">
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
            A personal recommendation system
          </p>
          <h1
            class="text-h3 mb-6"
            style="font-weight: 400; line-height: 1.2"
          >
            Five scents, chosen from what you already love.
          </h1>
          <p
            class="text-body-1"
            style="max-width: 520px; line-height: 1.8"
          >
            Each month, this tool reads your entire fragrance collection. What you own. What you like. What
            hasn't been right. Then it returns five picks that follow your taste's own logic forward,
            further into the territory your collection is already exploring.
          </p>
        </VCol>

        <VCol
          cols="12"
          md="6"
          order="1"
          order-md="2"
          class="mb-8 mb-md-0"
        >
          <VImg
            src="https://images.pexels.com/photos/8361538/pexels-photo-8361538.jpeg?auto=compress&cs=tinysrgb&w=960"
            :aspect-ratio="0.8"
            rounded="lg"
            cover
            referrerpolicy="no-referrer"
            alt="A single amber glass perfume bottle on a pale neutral surface, warm directional light"
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

    <!-- ─── HOW IT WORKS ─────────────────────────────────────────────────── -->
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
            The process
          </p>
          <h2
            class="text-h5 mb-4"
            style="font-weight: 400; line-height: 1.35"
          >
            A curatorial process, built one collection at a time.
          </h2>
          <p
            class="text-body-1 text-medium-emphasis"
            style="max-width: 560px; line-height: 1.8"
          >
            Four automated steps run in the background each month. What lands in your inbox is simpler than
            that: something considered, chosen from your own history.
          </p>
        </VCol>

        <VCol cols="12" md="8">
          <div
            v-for="(step, index) in steps"
            :key="step.number"
            class="d-flex ga-6"
            :class="{ 'mb-10': index < steps.length - 1 }"
          >
            <div
              class="text-h4 text-medium-emphasis flex-shrink-0"
              style="font-weight: 300; width: 1.6rem; line-height: 1.1"
            >
              {{ step.number }}
            </div>
            <div>
              <h3 class="text-h6 font-weight-medium mb-2">{{ step.title }}</h3>
              <p
                class="text-body-1 text-medium-emphasis"
                style="max-width: 460px; line-height: 1.8"
              >
                {{ step.body }}
              </p>
            </div>
          </div>
        </VCol>
      </VRow>
    </section>

    <!-- ─── STATS / CLAIMS ────────────────────────────────────────────────── -->
    <VSheet
      color="surface"
      rounded="lg"
      class="pa-8 pa-md-12 mb-16"
    >
      <p
        class="text-overline text-medium-emphasis text-center mb-8"
        style="letter-spacing: 0.12em"
      >
        What makes this different
      </p>
      <VRow justify="center">
        <VCol
          v-for="stat in stats"
          :key="stat.figure"
          cols="12"
          sm="4"
          class="text-center"
        >
          <div
            class="text-h3"
            style="font-weight: 300; color: rgb(var(--v-theme-primary))"
          >
            {{ stat.figure }}
          </div>
          <p
            class="text-body-2 text-medium-emphasis mt-2"
            style="max-width: 200px; margin-left: auto; margin-right: auto"
          >
            {{ stat.caption }}
          </p>
        </VCol>
      </VRow>
    </VSheet>

    <!-- ─── CLOSING ───────────────────────────────────────────────────────── -->
    <section class="mb-8">
      <VRow align="center">
        <VCol
          cols="12"
          md="7"
          order="2"
          order-md="1"
          class="pr-md-12"
        >
          <p
            class="text-overline text-medium-emphasis mb-3"
            style="letter-spacing: 0.12em"
          >
            Why this exists
          </p>
          <h2
            class="text-h5 mb-6"
            style="font-weight: 400; line-height: 1.35"
          >
            Good taste is specific. So is a good recommendation.
          </h2>
          <p
            class="text-body-1"
            style="max-width: 560px; line-height: 1.8"
          >
            Most discovery tools are built for audiences. They optimize for what most people like, which
            means they drift away from what makes your taste specific. This one runs the other way.
          </p>
          <p
            class="text-body-1 text-medium-emphasis mt-4"
            style="max-width: 560px; line-height: 1.8"
          >
            The longer your collection grows, the more precisely it knows what to look for: which woods you
            return to every autumn, which green things you keep setting aside. It gets better at this the
            same way you did, one rated fragrance at a time.
          </p>
        </VCol>

        <VCol
          cols="12"
          md="5"
          order="1"
          order-md="2"
          class="mb-8 mb-md-0"
        >
          <VImg
            src="https://images.pexels.com/photos/954405/pexels-photo-954405.jpeg?auto=compress&cs=tinysrgb&w=960"
            :aspect-ratio="1"
            rounded="lg"
            cover
            referrerpolicy="no-referrer"
            alt="A curated arrangement of glass perfume bottles on a table, warm natural side light"
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

  </div>
</template>

<script setup lang="ts">
definePageMeta({ title: 'About', layout: 'public', auth: false })

interface Step {
  readonly number: string
  readonly title: string
  readonly body: string
}

interface Stat {
  readonly figure: string
  readonly caption: string
}

const steps: readonly Step[] = [
  {
    number: '01',
    title: 'Learning what you love',
    body: "The full shape of your taste, what you own, what you like, what you've decided isn't for you, gets read and folded into a single, opinionated profile.",
  },
  {
    number: '02',
    title: 'Searching with direction',
    body: 'Two search angles, derived from that profile, go looking for the specific character your collection keeps reaching toward, the kind of thing that never makes a bestseller list.',
  },
  {
    number: '03',
    title: 'Curating the shortlist',
    body: 'Five candidates are selected, then each one verified: real, currently available, genuinely new to you. The system checks against everything it has recommended before and never repeats itself.',
  },
  {
    number: '04',
    title: 'Arriving in your inbox',
    body: 'A personal letter arrives by email. Five picks, each with its own reasoning, written around your actual collection.',
  },
]

const stats: readonly Stat[] = [
  { figure: '5', caption: 'picks, delivered every month, never the same fragrance twice' },
  { figure: '0', caption: "strangers' preferences in the model. Only your collection decides what shows up in your inbox." },
  { figure: '1', caption: 'owner. This tool was built for one collection and tuned for nothing else.' },
]
</script>
