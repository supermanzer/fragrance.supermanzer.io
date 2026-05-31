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
              primary:              '#4A6DB5',
              secondary:            '#6B9CB0',
              surface:              '#F3F6FA',
              background:           '#E6EDF4',
              error:                '#B25563',
              success:              '#3D7A6B',
              info:                 '#357A82',
              warning:              '#9E7B35',
              'on-surface':         '#111827',
              'on-surface-variant': '#4A5568',
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
