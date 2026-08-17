<template>
  <VCard class="pa-6 w-100" elevation="1" rounded="lg">
    <VFadeTransition mode="out-in">
      <div v-if="state === 'loading'" key="loading">
        <VSkeletonLoader type="heading, text, text, button" />
      </div>

      <div v-else-if="state === 'confirm'" key="confirm">
        <p class="text-h6 font-weight-regular mb-6">
          Mark <strong>{{ name }}</strong> by <strong>{{ house }}</strong> as
          {{ ratingLabel(previewAction!) }}?
        </p>
        <div class="d-flex justify-center mb-6">
          <VIcon
            :icon="ratingIcon(previewAction!)"
            :color="statusColor(previewAction!)"
            size="40"
          />
        </div>
        <VBtn
          type="button"
          color="primary"
          variant="flat"
          rounded="lg"
          block
          :loading="confirming"
          :disabled="confirming"
          @click="handleConfirm"
        >
          Confirm
        </VBtn>
        <p class="text-body-2 text-medium-emphasis mt-4">
          Not you? You can safely ignore this email — nothing changes unless you tap Confirm above.
        </p>
      </div>

      <div v-else-if="state === 'success'" key="success">
        <p class="text-h6 font-weight-regular mb-2">Saved to your collection.</p>
        <p class="text-body-2 text-medium-emphasis mb-6">
          {{ name }} is now marked as {{ ratingLabel(previewAction!) }} in your collection.
        </p>
        <VBtn
          variant="text"
          color="primary"
          rounded="lg"
          @click="navigateTo('/fragrance')"
        >
          Open the app
        </VBtn>
      </div>

      <div v-else-if="state === 'already-rated'" key="already-rated">
        <VAlert
          type="info"
          variant="tonal"
          rounded="lg"
          class="mb-6"
        >
          <template v-if="sameAction">
            You've already marked <strong>{{ name }}</strong> as {{ ratingLabel(ratedAs!) }}.
          </template>
          <template v-else>
            You've already rated <strong>{{ name }}</strong> — as {{ ratingLabel(ratedAs!) }}.
            Ratings can't be changed from this link, but you can update it directly in your collection.
          </template>
        </VAlert>
        <VBtn
          variant="text"
          color="primary"
          rounded="lg"
          @click="navigateTo('/fragrance')"
        >
          Open your collection
        </VBtn>
      </div>

      <div v-else-if="state === 'invalid-token'" key="invalid-token" class="text-center">
        <VIcon
          icon="mdi-link-off"
          size="48"
          class="text-medium-emphasis mb-4"
        />
        <p class="text-body-1">This link isn't valid anymore.</p>
        <p class="text-body-2 text-medium-emphasis mt-1">
          It may have expired or already been used. If you're trying to rate a recommendation, open
          the app and find it under Runs.
        </p>
        <VBtn
          variant="text"
          color="primary"
          class="mt-4"
          @click="navigateTo('/')"
        >
          Go to the app
        </VBtn>
      </div>

      <div v-else key="network-error" class="text-center">
        <VIcon
          icon="mdi-wifi-off"
          size="48"
          class="text-medium-emphasis mb-4"
        />
        <p class="text-body-1">Something went wrong loading this page.</p>
        <p class="text-body-2 text-medium-emphasis mt-1">
          That's on us, not your link. Give it another try.
        </p>
        <VBtn
          variant="text"
          color="primary"
          class="mt-4"
          @click="loadPreview"
        >
          Try again
        </VBtn>
      </div>
    </VFadeTransition>
  </VCard>
</template>

<script setup lang="ts">
import { ratingLabel, ratingIcon, statusColor, type RatingAction } from '~/utils/rating'

definePageMeta({ layout: 'auth', auth: false })

type ConfirmState =
  | 'loading'
  | 'confirm'
  | 'success'
  | 'already-rated'
  | 'invalid-token'
  | 'network-error'

interface PreviewResponse {
  name: string
  house: string
  action: RatingAction
  already_rated: boolean
  rated_as: RatingAction | null
}

interface ConfirmResponse {
  name: string
  house: string
  action: RatingAction
}

const config = useRuntimeConfig()
const route = useRoute()

const tokenParam = route.query.t
const token = typeof tokenParam === 'string' ? tokenParam : null

const state = ref<ConfirmState>('loading')
const name = ref('')
const house = ref('')
const previewAction = ref<RatingAction | null>(null)
const ratedAs = ref<RatingAction | null>(null)
const confirming = ref(false)

const sameAction = computed(
  () => previewAction.value !== null && ratedAs.value !== null && previewAction.value === ratedAs.value
)

async function loadPreview(): Promise<void> {
  if (!token) {
    state.value = 'invalid-token'
    return
  }
  state.value = 'loading'
  try {
    const data = await $fetch<PreviewResponse>('/recommendations/confirm/', {
      baseURL: config.public.apiBase,
      method: 'GET',
      query: { t: token },
    })
    name.value = data.name
    house.value = data.house
    previewAction.value = data.action
    if (data.already_rated) {
      ratedAs.value = data.rated_as
      state.value = 'already-rated'
    } else {
      state.value = 'confirm'
    }
  } catch (err: any) {
    state.value = err?.status === 404 ? 'invalid-token' : 'network-error'
  }
}

async function handleConfirm(): Promise<void> {
  if (!token) return
  confirming.value = true
  try {
    const data = await $fetch<ConfirmResponse>('/recommendations/confirm/', {
      baseURL: config.public.apiBase,
      method: 'POST',
      query: { t: token },
    })
    name.value = data.name
    house.value = data.house
    previewAction.value = data.action
    state.value = 'success'
  } catch (err: any) {
    if (err?.status === 409) {
      ratedAs.value = err?.data?.rated_as ?? null
      state.value = 'already-rated'
    } else if (err?.status === 404) {
      state.value = 'invalid-token'
    } else {
      state.value = 'network-error'
    }
  } finally {
    confirming.value = false
  }
}

onMounted(loadPreview)
</script>
