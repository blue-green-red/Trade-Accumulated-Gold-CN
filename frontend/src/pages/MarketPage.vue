<script setup>
import SubpageLayout from "../components/SubpageLayout.vue";
import { marketViews } from "../composables/dashboard";
import { lazy } from "../lazy";
const components = {
  prices: lazy(() => import("./market/PricePage.vue")),
  news: lazy(() => import("./market/NewsPage.vue")),
  calendar: lazy(() => import("./market/CalendarPage.vue")),
  reference: lazy(() => import("./market/ReferencePage.vue")),
};
</script>
<template>
  <SubpageLayout page="market" :views="marketViews" :components="components" />
</template>
