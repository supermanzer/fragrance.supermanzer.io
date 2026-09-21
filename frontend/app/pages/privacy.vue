<template>
  <LegalDocument
    title="Privacy Policy"
    overline="Legal"
    effective-date="[EFFECTIVE DATE]"
    version="1.0"
    :summary="summary"
    :sections="sections"
    :is-draft="IS_DRAFT"
  >
    <template #crosslink>
      The rules for using the service are in our
      <NuxtLink to="/terms" class="text-primary text-decoration-underline">Terms of Service</NuxtLink>.
    </template>
  </LegalDocument>
</template>

<script setup lang="ts">
import type { LegalSection } from '~/components/LegalDocument.vue'

definePageMeta({ layout: 'public', auth: false })

useSeoMeta({
  title: 'Privacy Policy',
  description:
    'What personal information Fragrances collects, why, who processes it on our behalf, how long we keep it, and how to ask for access or deletion.',
  robots: 'index, follow',
})

const IS_DRAFT = true

const summary: readonly string[] = [
  `We collect what's needed to run the service: your email, the fragrances and notes you enter, your recommendation history, and basic billing records. We never see your full card number.`,
  `To make recommendations we send your collection to an AI provider (Anthropic), and search the web through our own search engine. Emails go through Postmark and payments through Stripe.`,
  `We don't sell your data, we don't run advertising, and we don't use analytics trackers.`,
  `You can export your collection as a CSV at any time, and ask us to correct or delete your data by emailing [CONTACT EMAIL].`,
  `We use your browser's local storage to keep you signed in. There are no advertising or tracking cookies.`,
]

const sections: readonly LegalSection[] = [
  {
    id: 'who-we-are',
    title: '1. Who we are',
    blocks: [
      `[LEGAL NAME] ("we", "us") operates Fragrances at fragrances.supermanzer.io and is the controller of the personal information described here. Contact: [CONTACT EMAIL].`,
    ],
  },
  {
    id: 'information-we-collect',
    title: '2. Information we collect',
    blocks: [
      `Account information. Your email address (which also serves as your account name) and a password. Passwords are stored only as a salted hash, never in readable form. [CONFIRM: username collected separately?]`,
      `Your collection. For each fragrance you add: its name, house (brand), whether you own it, like it, or dislike it, and any notes you write. You can also import a collection from a CSV file.`,
      `Delivery settings. The email address recommendations are sent to and your schedule preferences (frequency and time).`,
      `Recommendation history. For each monthly run: the taste profile generated from your collection (loved, liked, and disliked notes, and two search phrases), the five fragrances recommended with the explanation for each, the full text of the email we sent you, its delivery status, and timestamps. This history also stops us recommending the same fragrance twice.`,
      `Waitlist information. When you join the beta waitlist, your email address, the time of your request, and its approval status. To reduce abuse, we also store a one-way hash of your IP address. It cannot be turned back into your IP address.`,
      `Billing information. A customer and subscription reference from Stripe, your subscription status, the plan, payment dates and amounts, and the dates your access covers. Card details are entered on Stripe's pages and go to Stripe; we do not receive or store them. We also keep a record of the event notifications Stripe sends us about your account so payments are applied correctly.`,
      `Technical information. When you use the Service, our servers and network providers process your IP address and standard request details (such as browser type and the pages requested). We use them to deliver the Service, limit abuse and repeated requests, and diagnose faults. [CONFIRM: web server log retention.]`,
      `Communications. If you email us, we keep the message and our reply.`,
    ],
  },
  {
    id: 'how-we-use-it',
    title: '3. How we use it',
    blocks: [
      {
        list: [
          `to run the Service: building your taste profile, finding and checking fragrances, and emailing you recommendations`,
          `to create and secure your account, and to send account emails such as invitations and password resets`,
          `to take payment, apply access to your account, and keep billing records`,
          `to prevent abuse and keep the Service secure, including rate limiting`,
          `to respond to your messages and fix problems`,
          `to meet legal, tax, and accounting obligations`,
        ],
      },
      `We don't use your collection to train our own models, we don't sell or rent your information, and we don't use it for advertising.`,
      { note: `[LEGAL BASES SECTION IF SERVING EEA/UK USERS: e.g. contract, legitimate interests, legal obligation. Counsel to decide.]` },
    ],
  },
  {
    id: 'who-we-share-it-with',
    title: '4. Who we share it with',
    blocks: [
      `We use a small number of service providers ("processors") who handle information on our behalf, only to provide their part of the Service:`,
      {
        list: [
          `Anthropic (AI model provider). Receives your collection (fragrance names, houses, statuses, and notes), the taste profile derived from it, search results, and candidate fragrances, so its models can write your profile, choose candidates, and write the email text. We do not send your email address or account details in these requests. Anthropic's privacy and data-use terms apply to what it receives.`,
          `Stripe (payments). Handles your payment details and subscription billing, and hosts the checkout and billing management pages.`,
          `Postmark (email delivery). Receives your email address and the content of the emails we send: invitations, password resets, and your recommendation emails.`,
          `Cloudflare (network and security). Sits in front of the site, so your requests, including your IP address, pass through Cloudflare before reaching our server.`,
          `DigitalOcean (hosting). Our servers, database, and backups run on DigitalOcean infrastructure.`,
        ],
      },
      `Web searches. To find new releases, we run searches on our own privately hosted search engine (SearXNG). It forwards the search phrases to public search engines. Those phrases are derived from the taste profile (for example, descriptions of note families) and the names of fragrances being checked. They do not include your name or email address, and we don't pass your IP address to those engines.`,
      `Images on our public pages. Some photographs on the public pages are loaded from Pexels, so when you visit those pages, your browser contacts Pexels and Pexels may see your IP address.`,
      `Legal and safety. We may disclose information if the law requires it, or to protect the rights and safety of users or the Service. If the business is sold or reorganised, information may transfer to the new owner under this policy.`,
    ],
  },
  {
    id: 'cookies-and-local-storage',
    title: '5. Cookies and local storage',
    blocks: [
      `We don't use advertising or analytics cookies, and we don't run third-party analytics.`,
      `When you sign in, your browser stores two sign-in tokens (an access token and a refresh token) in local storage. They are strictly necessary to keep you signed in and are removed when you sign out. They aren't used to track you.`,
      `Our administrative area (not used by members) sets ordinary session and security cookies. Stripe's and Cloudflare's own pages and services may set cookies of their own, as described in their policies.`,
    ],
  },
  {
    id: 'how-long-we-keep-information',
    title: '6. How long we keep information',
    blocks: [
      `We keep your account, collection, and recommendation history for as long as your account exists, including after a subscription lapses (in read-only mode), so that you can come back without starting over.`,
      `Stripe event records: the detailed notification content is removed after 90 days; a summary record (event type and time) is kept. Payment records needed for accounting are kept for [BILLING RECORDS RETENTION PERIOD, e.g. 7 years].`,
      `Waitlist entries: [WAITLIST RETENTION PERIOD, e.g. kept while pending or approved; rejected entries removed after N months].`,
      `Server and network logs: [LOG RETENTION PERIOD].`,
      `Backups: deleted data may remain in backups for up to [BACKUP RETENTION PERIOD] before being overwritten.`,
      `Emails you send us: [SUPPORT EMAIL RETENTION PERIOD].`,
    ],
  },
  {
    id: 'your-choices-and-rights',
    title: '7. Your choices and rights',
    blocks: [
      `You can view and edit your collection in the app, and export it as a CSV file at any time, including after a subscription lapses. You can change your delivery email and schedule in Settings.`,
      `You can ask us to give you a copy of your information, correct it, or delete your account and the data linked to it, by emailing [CONTACT EMAIL]. We'll respond within [RESPONSE TIME, e.g. 30 days]. We may need to keep limited records (for example, payment records) where the law requires.`,
      `Depending on where you live, you may also have rights to object to or restrict certain processing, to data portability, and to complain to your local data protection authority. [JURISDICTION-SPECIFIC RIGHTS SECTION, e.g. GDPR / UK GDPR / CCPA, per counsel.]`,
      `We can't currently delete your account from within the app; deletion is handled by request.`,
    ],
  },
  {
    id: 'security',
    title: '8. Security',
    blocks: [
      `We protect your information with measures including encrypted connections (HTTPS) end to end from your browser to Cloudflare and on to our server, hashed passwords, short-lived sign-in tokens that are rotated on use, rate limiting on sign-in and billing actions, and access restricted to the people who run the Service. Card details never touch our servers. No system is perfectly secure, and we can't guarantee absolute security. If a breach affecting your information occurs, we'll notify you as the law requires.`,
    ],
  },
  {
    id: 'international-transfers',
    title: '9. International transfers',
    blocks: [
      `Our providers may process information in countries other than yours, including the United States. [TRANSFER MECHANISM STATEMENT, per counsel, e.g. Standard Contractual Clauses.]`,
    ],
  },
  {
    id: 'children',
    title: '10. Children',
    blocks: [
      `The Service is not for anyone under 18, and we don't knowingly collect information from children. If you believe a child has given us information, tell us at [CONTACT EMAIL] and we'll delete it.`,
    ],
  },
  {
    id: 'changes-to-this-policy',
    title: '11. Changes to this policy',
    blocks: [
      `We may update this policy. For material changes we'll email you or show a notice at least [POLICY CHANGE NOTICE PERIOD] days before they take effect. The date and version above show the current version.`,
    ],
  },
  {
    id: 'contact',
    title: '12. Contact',
    blocks: [
      `Privacy questions or requests: [CONTACT EMAIL]. [LEGAL NAME], [POSTAL ADDRESS OR 'available on request'].`,
    ],
  },
]
</script>
