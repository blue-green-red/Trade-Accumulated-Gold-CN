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
const basis = computed(() => data.value?.decision_basis);
const metrics = computed(() => data.value?.scenario_metrics);
const weightedDownside = computed(() =>
  metrics.value && selected.value
    ? (
        (metrics.value.net_downside_pct * selected.value.target_position_pct) /
        100
      ).toFixed(2)
    : null,
);
const reasons = {
  expired: "已到期，等待下一轮分析",
  market_closed: "当前休市，方案仅供参考",
  quote_unavailable: "当前报价不可用，等待复核",
  price_changed: "行情已变化，等待复核",
  review_due: "已达到共用复评条件，等待下一轮分析",
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
    <div v-if="!basis && data.analysis.watch_conditions?.length" class="sub">
      复评条件：{{ data.analysis.watch_conditions.join("；") }}
    </div>
    <div v-if="basis" class="page-stack">
      <div>
        <strong>仓位取舍</strong>
        <p>{{ basis.position_rationale }}</p>
      </div>
      <div v-if="basis.entry_scenario">
        <strong>假设入场情景</strong>
        <div>
          入场 {{ basis.entry_scenario.entry_price_cny }} · 目标
          {{ basis.entry_scenario.target_price_cny }} · 退出
          {{ basis.entry_scenario.exit_price_cny }} 元/克
        </div>
        <div class="sub">{{ basis.entry_scenario.basis }}</div>
        <div v-if="metrics" class="sub">
          卖费参考 {{ (metrics.sell_fee_rate * 100).toFixed(2) }}% ·
          扣费目标收益 {{ metrics.net_upside_pct }}% · 退出收益
          {{ metrics.net_downside_pct }}%
          <span v-if="metrics.reward_risk_ratio != null">
            · 收益风险比 {{ metrics.reward_risk_ratio }}</span
          >
        </div>
        <div class="sub">仅用于比较机会，不代表建议买入或实际持仓盈亏。</div>
      </div>
      <div v-else class="sub">未给入场情景：{{ basis.no_entry_reason }}</div>
      <div>
        <strong>共用复评条件</strong>
        <ul class="list">
          <li v-for="(trigger, i) in basis.review_triggers" :key="i">
            <template v-if="trigger.kind === 'price'">
              公共京东参考价
              {{ trigger.condition === "at_or_above" ? "≥" : "≤" }}
              {{ trigger.price_cny }} 元/克
            </template>
            <template v-else-if="trigger.kind === 'event'"
              >{{ trigger.event
              }}<span v-if="trigger.at">
                · {{ fmtTs(trigger.at) }}</span
              ></template
            >
            <template v-else>
              {{ fmtTs(trigger.at) }} 定时复评<span v-if="trigger.event">
                · {{ trigger.event }}</span
              >
            </template>
          </li>
        </ul>
      </div>
      <div v-if="basis.no_conditions_reason" class="sub">
        未推荐条件方案：{{ basis.no_conditions_reason }}
      </div>
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
      <div v-if="weightedDownside != null" class="sub">
        假设按该目标仓位新建仓，退出情景对黄金投资资金的影响约
        {{ weightedDownside }}%；不代表存量持仓盈亏。
      </div>
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
