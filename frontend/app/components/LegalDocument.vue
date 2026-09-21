<template>
  <div class="pt-4 pt-md-8">
    <v-row>
      <v-col cols="3" class="d-none d-md-block">
        <div style="position: sticky; top: 88px">
          <nav aria-label="On this page">
            <div class="text-overline text-medium-emphasis mb-2" style="letter-spacing: 0.12em">
              On this page
            </div>
            <v-list density="compact" nav bg-color="transparent">
              <v-list-item
                v-for="section in sections"
                :key="section.id"
                :href="'#' + section.id"
                :title="section.title"
                rounded="lg"
                min-height="32"
                class="text-body-2"
              />
            </v-list>
          </nav>
        </div>
      </v-col>

      <v-col cols="12" md="9" lg="8">
        <v-alert
          v-if="isDraft"
          type="warning"
          variant="tonal"
          rounded="lg"
          density="comfortable"
          class="mb-8"
        >
          Draft. This document is not yet in effect and contains placeholders.
        </v-alert>

        <div class="text-overline text-medium-emphasis mb-3" style="letter-spacing: 0.12em">
          {{ overline }}
        </div>
        <h1 class="text-h4 mb-2" style="font-weight: 400; line-height: 1.25">{{ title }}</h1>
        <div class="text-caption text-medium-emphasis mb-8">
          Effective {{ effectiveDate }} &middot; Version {{ version }}
        </div>

        <v-sheet color="surface" rounded="lg" class="pa-6 pa-md-8 mb-10">
          <div class="text-overline text-medium-emphasis mb-3" style="letter-spacing: 0.12em">
            In short
          </div>
          <ul class="pl-6">
            <li
              v-for="(item, i) in summary"
              :key="i"
              class="text-body-1"
              style="line-height: 1.7"
            >
              {{ item }}
            </li>
          </ul>
        </v-sheet>

        <v-expansion-panels variant="accordion" flat class="d-md-none mb-8">
          <v-expansion-panel title="On this page" bg-color="transparent">
            <v-expansion-panel-text>
              <nav aria-label="On this page">
                <v-list density="compact" nav bg-color="transparent">
                  <v-list-item
                    v-for="section in sections"
                    :key="section.id"
                    :href="'#' + section.id"
                    :title="section.title"
                    rounded="lg"
                    min-height="32"
                    class="text-body-2"
                  />
                </v-list>
              </nav>
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>

        <section
          v-for="(section, index) in sections"
          :id="section.id"
          :key="section.id"
          style="scroll-margin-top: 88px"
        >
          <h2
            class="text-h6 font-weight-medium mb-3"
            :class="index === 0 ? 'mt-0' : 'mt-10'"
            style="line-height: 1.4"
          >
            {{ section.title }}
          </h2>
          <template v-for="(block, bi) in section.blocks" :key="bi">
            <p
              v-if="typeof block === 'string'"
              class="text-body-1 mb-4"
              style="line-height: 1.8; max-width: 680px"
            >
              {{ block }}
            </p>
            <ul
              v-else-if="'list' in block"
              class="pl-6 mb-4 text-body-1"
              style="line-height: 1.8; max-width: 680px"
            >
              <li v-for="(li, i) in block.list" :key="i">{{ li }}</li>
            </ul>
            <p
              v-else
              class="text-body-2 text-medium-emphasis mb-4"
              style="max-width: 680px"
            >
              {{ block.note }}
            </p>
          </template>
        </section>

        <div class="mt-12">
          <p class="text-body-1" style="line-height: 1.8">
            <slot name="crosslink" />
          </p>
          <v-btn
            variant="text"
            size="small"
            to="/"
            prepend-icon="mdi-arrow-left"
            class="mt-4"
          >
            Back to home
          </v-btn>
        </div>
      </v-col>
    </v-row>
  </div>
</template>

<script setup lang="ts">
export type LegalBlock = string | { readonly list: readonly string[] } | { readonly note: string }

export interface LegalSection {
  readonly id: string
  readonly title: string
  readonly blocks: readonly LegalBlock[]
}

defineProps<{
  readonly title: string
  readonly overline: string
  readonly effectiveDate: string
  readonly version: string
  readonly summary: readonly string[]
  readonly sections: readonly LegalSection[]
  readonly isDraft: boolean
}>()
</script>
