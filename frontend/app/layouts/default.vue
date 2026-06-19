<template>
  <v-app-bar elevation="0" border="b">
    <v-app-bar-title>Fragrances</v-app-bar-title>

    <v-btn :to="{ path: '/fragrance' }" variant="text" active-color="primary">Collection</v-btn>
    <v-btn :to="{ path: '/runs' }" variant="text" active-color="primary">Runs</v-btn>
    <v-btn :to="{ path: '/profile' }" variant="text" active-color="primary">Profile</v-btn>
    <v-btn :to="{ path: '/settings' }" variant="text" active-color="primary">Settings</v-btn>

    <template #append>
      <v-btn icon @click="logout">
        <v-icon>mdi-logout</v-icon>
      </v-btn>
    </template>
  </v-app-bar>

  <v-main>
    <v-container class="pt-8">
      <slot />
    </v-container>
  </v-main>
</template>

<script setup lang="ts">
import { clearTokens } from '~/composables/useApi'

async function logout() {
  clearTokens()
  await navigateTo('/auth/login')
}
</script>
