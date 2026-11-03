<script setup>
import { fmt } from "../state.js";
defineProps({ levels: { type: Array, default: () => [] } });
</script>
<template>
  <div v-if="levels.length" class="key-levels">
    <article v-for="(level, i) in levels" :key="i">
      <div>
        <span>{{ level.label }}</span
        ><strong>{{ fmt(level.price_cny) }}<small>元/克</small></strong>
      </div>
      <p class="muted small">{{ level.basis }}</p>
    </article>
    <div class="muted small">
      采样值不是完整K线的技术位；模型情景与复评阈值均不代表必然到达。
    </div>
  </div>
  <p v-else class="muted small">本轮暂无通过公开校验的关键价位。</p>
</template>
