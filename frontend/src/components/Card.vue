<script setup>
defineProps({ title: String, subtitle: String });
</script>
<template>
  <section class="card">
    <div v-if="title || subtitle || $slots.action" class="card-head">
      <h2 v-if="title" class="card-title">{{ title }}</h2>
      <span v-if="subtitle" class="sub">{{ subtitle }}</span
      ><slot name="action" />
    </div>
    <slot />
  </section>
</template>
