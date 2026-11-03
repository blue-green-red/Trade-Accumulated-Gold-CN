<script setup>
import { navigate } from "../composables/dashboard";
import { lazy } from "../lazy";
const SystemDiagnostics = lazy(
  () => import("../components/SystemDiagnostics.vue"),
);
</script>
<template>
  <section id="panel-diagnostics" class="page-stack" data-page-ready="true">
    <button class="link-btn back-link" @click="navigate('me')">
      ‹ 返回我的
    </button>
    <div id="system-diagnostics"><SystemDiagnostics /></div>
  </section>
</template>
