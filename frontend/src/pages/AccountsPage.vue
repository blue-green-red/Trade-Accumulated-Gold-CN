<script setup>
import SubpageLayout from "../components/SubpageLayout.vue";
import { accountViews, accountView } from "../composables/dashboard";
import { lazy } from "../lazy";
const live = lazy(() => import("./accounts/LiveList.vue"));
const bank = lazy(() => import("./accounts/BankDetail.vue"));
const resolve = (view, data) => (view.startsWith("live/") ? bank : live);
</script>
<template>
  <SubpageLayout
    page="strategies"
    :views="accountViews"
    :resolve="resolve"
    :hide-tabs="accountView.includes('/')"
  />
</template>
