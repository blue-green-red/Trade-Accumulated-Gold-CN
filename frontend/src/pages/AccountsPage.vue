<script setup>
import SubpageLayout from "../components/SubpageLayout.vue";
import { accountViews, accountView } from "../composables/dashboard";
import { lazy } from "../lazy";
const live = lazy(() => import("./accounts/LiveList.vue")),
  simulation = lazy(() => import("./accounts/SimulationList.vue"));
const bank = lazy(() => import("./accounts/BankDetail.vue")),
  portfolio = lazy(() => import("./accounts/PortfolioDetail.vue"));
const resolve = (view, data) =>
  view.startsWith("live/")
    ? bank
    : view.startsWith("simulation/")
      ? portfolio
      : (view === "auto" ? data?.account_view : view) === "live"
        ? live
        : simulation;
</script>
<template>
  <SubpageLayout
    page="strategies"
    :views="accountViews"
    :resolve="resolve"
    :hide-tabs="accountView.includes('/')"
  />
</template>
