<script setup>
import { time } from "../state.js";
defineProps({ triggers: { type: Array, default: () => [] } });
</script>
<template>
  <ul class="review-list">
    <li v-for="(trigger, i) in triggers" :key="i">
      <span class="review-dot"></span
      ><span>
        <template v-if="trigger.kind === 'price'"
          >京东参考价 {{ trigger.condition === "at_or_above" ? "≥" : "≤" }}
          <strong>{{ trigger.price_cny }} 元/克</strong></template
        >
        <template v-else
          ><strong v-if="trigger.at">{{ time(trigger.at) }}</strong>
          {{ trigger.event || "定时复评" }}</template
        >
      </span>
    </li>
  </ul>
</template>
