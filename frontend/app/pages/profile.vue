<template>
  <div>
    <h1 class="text-h4 mb-6">Preference Profile</h1>

    <AppAlert v-model="error" class="mb-4" />

    <v-skeleton-loader v-if="loading" type="article" />

    <v-alert v-else-if="!profile" variant="tonal" class="mb-6">
      <template #prepend>
        <v-icon>mdi-flask-outline</v-icon>
      </template>
      No profile yet. One is generated automatically when your first recommendation run completes.
    </v-alert>

    <template v-else>
      <p class="text-caption text-medium-emphasis mb-6">
        Generated {{ new Date(profile.generated_at).toLocaleString() }}
      </p>

      <v-row class="mb-2">
        <v-col cols="12" md="4">
          <v-card height="100%" elevation="1" rounded="lg">
            <v-card-title class="pt-6 px-6 pb-2">
              <v-icon size="18" class="mr-2" color="primary">mdi-heart-outline</v-icon>
              Loved Notes
            </v-card-title>
            <v-card-text class="px-6 pb-6">{{ profile.loved_notes }}</v-card-text>
          </v-card>
        </v-col>
        <v-col cols="12" md="4">
          <v-card height="100%" elevation="1" rounded="lg">
            <v-card-title class="pt-6 px-6 pb-2">
              <v-icon size="18" class="mr-2" color="secondary">mdi-thumb-up-outline</v-icon>
              Liked Notes
            </v-card-title>
            <v-card-text class="px-6 pb-6">{{ profile.liked_notes }}</v-card-text>
          </v-card>
        </v-col>
        <v-col cols="12" md="4">
          <v-card height="100%" elevation="1" rounded="lg">
            <v-card-title class="pt-6 px-6 pb-2">
              <v-icon size="18" class="mr-2 text-medium-emphasis">mdi-thumb-down-outline</v-icon>
              Disliked Notes
            </v-card-title>
            <v-card-text class="px-6 pb-6">{{ profile.disliked_notes }}</v-card-text>
          </v-card>
        </v-col>
      </v-row>

      <v-card class="mt-6" elevation="1" rounded="lg">
        <v-card-title class="pt-6 px-6 pb-2">Search Angles</v-card-title>
        <v-card-text class="px-6 pb-6">
          <p class="text-body-2 mb-3"><strong>Angle 1:</strong> {{ profile.search_angle_1 }}</p>
          <p class="text-body-2"><strong>Angle 2:</strong> {{ profile.search_angle_2 }}</p>
        </v-card-text>
      </v-card>

      <v-card class="mt-6" elevation="1" rounded="lg">
        <v-card-title class="pt-6 px-6 pb-2">Fragrances You Own</v-card-title>
        <v-card-text class="px-6 pb-6">
          <v-chip
            v-for="name in profile.owns_list"
            :key="name"
            variant="outlined"
            class="mr-2 mb-2"
          >
            {{ name }}
          </v-chip>
          <span v-if="!profile.owns_list.length" class="text-body-2 text-medium-emphasis">
            None recorded.
          </span>
        </v-card-text>
      </v-card>
    </template>
  </div>
</template>

<script setup lang="ts">
import { useProfile } from '~/composables/useProfile'

const { profile, loading, error, fetchProfile } = useProfile()
onMounted(fetchProfile)
</script>
