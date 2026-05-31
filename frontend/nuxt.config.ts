// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  devtools: { enabled: true },
  modules: ['vuetify-nuxt-module'],
  css: ['@mdi/font/css/materialdesignicons.css'],
  vuetify: {
    vuetifyOptions: {
      icons: { defaultSet: 'mdi' },
      theme: {
        defaultTheme: 'fragrance',
        themes: {
          fragrance: {
            dark: false,
            colors: {
              primary:              '#5C4A3A',
              secondary:            '#8C7B6B',
              surface:              '#FAF8F5',
              background:           '#F2EFE9',
              error:                '#8B3A2E',
              success:              '#4A6741',
              info:                 '#3A5A6E',
              warning:              '#7A5C2E',
              'on-surface':         '#1A1612',
              'on-surface-variant': '#6E5E50',
            },
          },
        },
      },
    },
  },
  routeRules: {
    '/': { redirect: '/fragrance' },
  },
  runtimeConfig: {
    public: {
      apiBase: process.env.NUXT_PUBLIC_API_BASE ?? 'http://localhost:8000/api/v1',
    },
  },
  vite: {
    optimizeDeps: {
      include: [
        '@vue/devtools-core',
        '@vue/devtools-kit',
      ]
    }
  }
})
