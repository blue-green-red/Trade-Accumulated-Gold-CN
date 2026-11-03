<script setup>
import SubpageLayout from "../components/SubpageLayout.vue";
import { notificationViews } from "../composables/dashboard";
import { lazy } from "../lazy";
const components = {
  settings: lazy(() => import("./notifications/SettingsPage.vue")),
  history: lazy(() => import("./notifications/HistoryPage.vue")),
};
</script>
<template>
  <SubpageLayout
    page="notifications"
    :views="notificationViews"
    :components="components"
  />
</template>
