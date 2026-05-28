export default defineNuxtRouteMiddleware((to) => {
  if (!import.meta.client) return
  if (to.path.startsWith('/auth/')) return

  const token = localStorage.getItem('auth_access')
  if (!token) return navigateTo('/auth/login')
})
