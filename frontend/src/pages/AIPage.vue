<script setup>
import SubpageLayout from "../components/SubpageLayout.vue";
import { aiViews } from "../composables/dashboard";
import { lazy } from "../lazy";
const components = {
  research: lazy(() => import("./ai/ResearchPage.vue")),
  control: lazy(() => import("./ai/ControlPage.vue")),
  evaluation: lazy(() => import("./ai/EvaluationPage.vue")),
};
</script>
<template>
  <SubpageLayout page="ai" :views="aiViews" :components="components" />
</template>
