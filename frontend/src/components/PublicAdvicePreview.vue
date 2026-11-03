<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { actions, fmtTs } from "../format";
const props = defineProps({ analyst: String });
const data = ref(null);
const error = ref(false);
const position = ref(0);
const clock = ref(Date.now());
let timer;
const displayReason = computed(() =>
  Date.parse(data.value?.expires_at) <= clock.value
    ? "expired"
    : data.value?.reason,
);
const selected = computed(() =>
  data.value?.recommendations?.find((r) => r.position_pct === position.value),
);
const reasons = {
  expired: "已到期，等待下一轮分析",
  market_closed: "当前休市，方案仅供参考",
  quote_unavailable: "当前报价不可用，等待复核",
  price_changed: "行情已变化，等待复核",
};
onMounted(async () => {
  timer = setInterval(() => (clock.value = Date.now()), 1000);
  try {
    const response = await fetch(
      `/api/public/ai?analyst=${encodeURIComponent(props.analyst)}`,
      { cache: "no-cache", signal: AbortSignal.timeout(15000) },
    );
    if (!response.ok) throw new Error("unavailable");
    data.value = await response.json();
  } catch {
    error.value = true;
  }
});
onUnmounted(() => clearInterval(timer));
</script>
<template>
  <div v-if="error" class="empty">公众内容暂时加载失败</div>
  <div v-else-if="!data" class="empty">正在读取已保存的公众内容…</div>
  <div v-else-if="!data.analysis" class="empty">
    本轮尚无通过发布校验的公众内容，等待后续分析。
  </div>
  <div v-else class="page-stack">
    <div class="sub">
      高风险 · {{ data.policy.horizon }} · {{ fmtTs(data.created_at) }}
    </div>
    <div v-if="data.status !== 'current' || displayReason" class="notice warn">
      {{ reasons[displayReason] || "该方案待复核" }}
    </div>
    <p>{{ data.analysis.outlook }}</p>
    <div class="sub">本轮变化：{{ data.analysis.change_summary }}</div>
    <ul class="list">
      <li v-for="driver in data.analysis.drivers" :key="driver">
        {{ driver }}
      </li>
    </ul>
    <div>风险：{{ data.analysis.risk }}</div>
    <div class="sub">
      复评条件：{{ data.analysis.watch_conditions.join("；") }}
    </div>
    <label>
      当前黄金仓位
      <select v-model="position" aria-label="公众推荐当前仓位">
        <option v-for="n in 11" :key="n" :value="(n - 1) * 10">
          {{ (n - 1) * 10 }}%
        </option>
      </select>
    </label>
    <div class="sub">仓位口径：{{ data.policy.position_basis }}</div>
    <div v-if="selected">
      <strong>
        {{ actions[selected.action] || selected.action }} ·
        {{ selected.position_pct }}% → {{ selected.target_position_pct }}%
      </strong>
      <p>{{ selected.reason }}</p>
      <ul v-if="selected.conditions.length" class="list">
        <li v-for="(order, i) in selected.conditions" :key="i">
          若公共京东参考价
          {{ order.condition === "at_or_above" ? "≥" : "≤" }}
          {{ order.price_cny }} 元/克，{{ actions[order.action] }}至
          {{ order.target_position_pct }}%
        </li>
      </ul>
      <div class="sub">失效条件：{{ selected.invalidation }}</div>
    </div>
    <div class="sub">
      方案有效至 {{ fmtTs(data.expires_at) }}；切换仓位仅查看缓存。
    </div>
  </div>
</template>
