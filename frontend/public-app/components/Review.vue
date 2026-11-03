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
          ><template v-if="trigger.at"
            >建议关注时间：<strong>{{ time(trigger.at) }}</strong></template
          >
          {{ trigger.event || "届时关注行情与观点变化" }}</template
        >
      </span>
    </li>
  </ul>
</template>
