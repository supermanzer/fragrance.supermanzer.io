export default defineNuxtRouteMiddleware((to) => {
  if (!import.meta.client) return
  if (to.meta.auth === false) return

  const token = localStorage.getItem('auth_access')
  if (!token) return navigateTo('/auth/login')
})
