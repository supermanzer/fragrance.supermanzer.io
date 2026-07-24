<template>
  <v-app-bar elevation="0" color="background">
    <v-app-bar-title>
      <NuxtLink to="/" class="text-decoration-none">
        <div
          class="text-h5 font-weight-regular text-primary"
          style="letter-spacing: 0.06em"
        >
          Fragrances
        </div>
      </NuxtLink>
    </v-app-bar-title>

    <template #append>
      <v-btn variant="text" size="default" to="/about">About</v-btn>
      <v-btn variant="text" size="default" to="/auth/login" class="ml-2">Sign in</v-btn>
    </template>
  </v-app-bar>

  <v-main>
    <v-container class="pt-8">
      <slot />
    </v-container>
  </v-main>

  <v-divider class="mb-0" />
  <v-footer color="background" class="mt-16 pt-8 pb-6">
    <v-container>
      <!-- xs/sm: stacked, centered -->
      <div class="text-center d-md-none">
        <div
          class="text-caption font-weight-medium"
          style="letter-spacing: 0.1em"
        >
          Fragrances
        </div>
        <div class="text-caption text-medium-emphasis">
          your personal scent journal
        </div>
        <div class="d-flex justify-center ga-2 mt-3">
          <v-btn
            variant="text"
            size="small"
            to="/about"
            class="text-decoration-underline"
          >
            About
          </v-btn>
          <v-btn
            variant="text"
            size="small"
            to="/auth/login"
            class="text-decoration-underline"
          >
            Sign in
          </v-btn>
        </div>
      </div>

      <!-- md+: single row, wordmark/tagline left, links right -->
      <div class="d-none d-md-flex justify-space-between align-center">
        <div>
          <div
            class="text-caption font-weight-medium"
            style="letter-spacing: 0.1em"
          >
            Fragrances
          </div>
          <div class="text-caption text-medium-emphasis">
            your personal scent journal
          </div>
        </div>
        <div class="d-flex ga-4">
          <v-btn
            variant="text"
            size="small"
            to="/about"
            class="text-decoration-underline"
          >
            About
          </v-btn>
          <v-btn
            variant="text"
            size="small"
            to="/auth/login"
            class="text-decoration-underline"
          >
            Sign in
          </v-btn>
        </div>
      </div>
    </v-container>
  </v-footer>
</template>
