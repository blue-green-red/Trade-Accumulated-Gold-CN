<script setup>
defineProps({ name: String });
const icons = {
  overview:
    '<path d="M3 11.5 12 4l9 7.5V20a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
  strategies: '<path d="M4 19V9m6 10V5m6 14v-7m4 7H2"/>',
  compare: '<path d="M5 20V10m7 10V4m7 16V7M2 20h20"/>',
  ai: '<path d="M12 3v3m0 12v3m9-9h-3M6 12H3m14.5-6.5-2 2m-7 7-2 2m11 0-2-2m-7-7-2-2"/><circle cx="12" cy="12" r="3.2"/>',
  market: '<path d="M3 17l5-6 4 3 6-8 3 4"/><path d="M3 21h18"/>',
  notifications:
    '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M10 21h4"/>',
  records: '<path d="M6 3h9l4 4v14H6z"/><path d="M9 11h7M9 15h7M9 7h3"/>',
};
</script>
<template>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="icons[name] || icons.overview"
  />
</template>
