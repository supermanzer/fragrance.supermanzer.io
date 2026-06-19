<template>
  <v-main class="fill-height">
    <v-container class="fill-height" max-width="480">
      <v-row justify="center" align="center" class="fill-height">
        <v-col cols="12">
          <div class="text-center mb-8">
            <div
              class="text-h4 font-weight-regular mb-1"
              style="color: #4A6DB5; letter-spacing: 0.06em"
            >
              Fragrances
            </div>
            <div class="text-caption text-medium-emphasis" style="letter-spacing: 0.1em">
              your personal scent journal
            </div>
          </div>
          <slot />
        </v-col>
      </v-row>
    </v-container>
  </v-main>
</template>
