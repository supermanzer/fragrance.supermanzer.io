<template>
  <div>
    <h1 class="text-h4 mb-6">Preference Profile</h1>

    <AppAlert v-model="error" class="mb-4" />

    <v-skeleton-loader v-if="loading" type="article" />

    <v-alert v-else-if="!profile" type="info" variant="tonal">
      No profile has been generated yet. A profile is created automatically when your first recommendation run executes.
    </v-alert>

    <template v-else>
      <p class="text-caption text-medium-emphasis mb-6">
        Generated {{ new Date(profile.generated_at).toLocaleString() }}
      </p>

      <v-row>
        <v-col cols="12" md="4">
          <v-card height="100%">
            <v-card-title class="text-success">Loved Notes</v-card-title>
            <v-card-text>{{ profile.loved_notes }}</v-card-text>
          </v-card>
        </v-col>
        <v-col cols="12" md="4">
          <v-card height="100%">
            <v-card-title class="text-info">Liked Notes</v-card-title>
            <v-card-text>{{ profile.liked_notes }}</v-card-text>
          </v-card>
        </v-col>
        <v-col cols="12" md="4">
          <v-card height="100%">
            <v-card-title class="text-error">Disliked Notes</v-card-title>
            <v-card-text>{{ profile.disliked_notes }}</v-card-text>
          </v-card>
        </v-col>
      </v-row>

      <v-card class="mt-4">
        <v-card-title>Search Angles</v-card-title>
        <v-card-text>
          <p class="mb-2"><strong>Angle 1:</strong> {{ profile.search_angle_1 }}</p>
          <p><strong>Angle 2:</strong> {{ profile.search_angle_2 }}</p>
        </v-card-text>
      </v-card>

      <v-card class="mt-4">
        <v-card-title>Fragrances You Own</v-card-title>
        <v-card-text>
          <v-chip v-for="name in profile.owns_list" :key="name" class="mr-2 mb-2">{{ name }}</v-chip>
          <span v-if="!profile.owns_list.length" class="text-medium-emphasis">None recorded.</span>
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
