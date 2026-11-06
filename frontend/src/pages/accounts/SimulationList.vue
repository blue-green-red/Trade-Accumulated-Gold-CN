<script setup>
import { computed, ref, watch, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
import PnlBreakdown from "../../components/PnlBreakdown.vue";
import {
  openPortfolio,
  navigate,
  selectView,
} from "../../composables/dashboard";
import {
  totals,
  modelChoices,
  periodChoices,
  modelNames,
  accountLabels,
  num,
  money,
  pct,
} from "../../format";
const props = defineProps({ data: Object });
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
const model = ref("all"),
  risk = ref("all");
watch(
  () => props.data.model_controls,
  () => {
    if (
      model.value !== "all" &&
      !props.data.model_controls?.some(
        (c) => c.enabled && c.key === model.value,
      )
    )
      model.value = "all";
  },
);
const accounts = computed(() =>
  (props.data.leaderboard || []).filter(
    (a) =>
      (model.value === "all" || a.model === model.value) &&
      (risk.value === "all" || a.start_mode.startsWith(risk.value + "_")),
  ),
);
const summary = computed(() => totals(accounts.value));
const groups = computed(() =>
  modelChoices(props.data)
    .slice(1)
    .filter(([m]) => model.value === "all" || model.value === m)
    .flatMap(([m, name]) =>
      periodChoices(props.data)
        .slice(1)
        .filter(([r]) => risk.value === "all" || risk.value === r)
        .map(([r, label]) => ({
          key: m + ":" + r,
          name: name + " · " + label,
          rows: accounts.value
            .filter((a) => a.model === m && a.start_mode.startsWith(r + "_"))
            .sort(
              (a, b) =>
                Number(b.start_mode.endsWith("_full")) -
                Number(a.start_mode.endsWith("_full")),
            ),
        })),
    ),
);
</script>
<template>
  <div class="page-stack">
    <Card
      title="模拟账户 · 三档风险 × 两种起点"
      subtitle="每个模型六户独立资金 · 每户初始10万元"
    >
      <p class="sub">
        同一风险档内对照“初始满仓”和“初始空仓”；标签描述开账时的起点，当前仓位随交易变化。真实银行资金另计。
      </p>
      <div class="asset-actions">
        <button class="chip-btn" @click="navigate('compare')">成绩对比 →</button
        ><button
          class="link-btn"
          @click="
            navigate('records', {
              kind: 'fills',
              model,
              account: 'all',
              days: 'all',
            })
          "
        >
          操作记录 →
        </button>
      </div>
      <div class="filters">
        <label
          >账户模型<select v-model="model" aria-label="账户模型">
            <option
              v-for="[key, label] in modelChoices(data)"
              :key="key"
              :value="key"
            >
              {{ label }}
            </option>
          </select></label
        ><label
          >风险场景<select v-model="risk" aria-label="风险场景">
            <option
              v-for="[key, label] in periodChoices(data)"
              :key="key"
              :value="key"
            >
              {{ label }}
            </option>
          </select></label
        >
      </div>
      <div class="mini-stats">
        <div class="mini-stat">
          <div class="l">筛选内净资产 · {{ summary.started }} 户</div>
          <div class="v">{{ money(summary.value) }}</div>
        </div>
        <div class="mini-stat">
          <div class="l">累计盈亏</div>
          <div class="v">{{ money(summary.pnl, true) }}</div>
        </div>
        <div class="mini-stat">
          <div class="l">可用现金</div>
          <div class="v">{{ money(summary.cash) }}</div>
        </div>
      </div>
      <details v-if="summary.started" class="more">
        <summary>筛选账户的盈亏归因</summary>
        <PnlBreakdown :value="summary.pnl_breakdown" /></details
    ></Card>
    <Card
      v-for="g in groups"
      :key="g.key"
      :title="g.name"
      subtitle="相同风险预算 · 满仓与空仓起点对照"
      ><div class="account-pair">
        <button
          v-for="a in g.rows"
          :key="a.key"
          class="account-preview"
          @click="openPortfolio(a.key)"
        >
          <div class="row">
            <strong>{{
              a.start_mode.endsWith("_full") ? "初始满仓" : "初始空仓"
            }}</strong
            ><span class="tag">{{
              !a.started ? "等待开账" : a.grams > 1e-9 ? "当前持仓" : "当前空仓"
            }}</span>
          </div>
          <div class="preview-value">
            {{ a.started ? money(a.value) : "未开账" }}
          </div>
          <div class="sub">
            累计 {{ pct(a.return) }} · 当前仓位
            {{ a.started ? num(a.position_pct) + "%" : "—" }}
          </div>
          <div class="sub">现金 {{ money(a.cash) }}</div>
          <div class="sub">持有 {{ num(a.grams, 3) }} 克</div>
          <span class="link-btn">持仓与最新决策 →</span>
        </button>
      </div></Card
    >
    <Card v-if="!accounts.length" title="模拟账户"
      ><div class="empty">暂无已开启模型的账户</div>
      <button class="link-btn" @click="selectView('ai', 'control')">
        开启模型 →
      </button></Card
    >
  </div>
</template>
