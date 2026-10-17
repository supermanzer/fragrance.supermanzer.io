<template>
  <RecipientPanel purpose="receipt" />
</template>

<script setup lang="ts">
// The "Receipt email" section on /billing. Mounted only where the account has a Stripe
// customer and is neither exempt nor locked; the shared panel also hides itself if the server
// answers exempt / account_locked / no_stripe_customer.
</script>
