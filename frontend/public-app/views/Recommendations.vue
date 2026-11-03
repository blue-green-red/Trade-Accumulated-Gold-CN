<script setup>
import { ref, computed } from "vue";
import {
  report,
  loadReport,
  reportLoading,
  reportStatus,
  fmt,
  time,
  actions,
} from "../state.js";
import { usePage } from "../usePage.js";
import FeedState from "../components/FeedState.vue";
import Review from "../components/Review.vue";
function initialPosition() {
  try {
    const n = Number(localStorage.getItem("gold-public-position"));
    return Number.isInteger(n) && n >= 0 && n <= 100 && n % 10 === 0 ? n : 0;
  } catch {
    return 0;
  }
}
const position = ref(initialPosition());
function choose(p) {
  position.value = p;
  try {
    localStorage.setItem("gold-public-position", String(p));
  } catch {}
}
const recommendation = computed(() =>
  report.value?.recommendations.find((r) => r.position_pct === position.value),
);
const basis = computed(() => report.value?.decision_basis);
const metrics = computed(() => report.value?.scenario_metrics);
const difference = computed(
  () => (recommendation.value?.target_position_pct || 0) - position.value,
);
const weightedRisk = computed(() =>
  metrics.value && recommendation.value
    ? (metrics.value.net_downside_pct *
        recommendation.value.target_position_pct) /
      100
    : null,
);
usePage(loadReport);
</script>
<template>
  <div
    class="view-stack"
    data-public-view="recommendations"
    :data-ready="report !== null"
    :aria-busy="reportLoading"
  >
    <div class="position-selector card">
      <div class="card-kicker">
        <span>选择当前黄金仓位</span><span class="risk-label">高风险参考</span>
      </div>
      <div class="position-current">{{ position }}<small>%</small></div>
      <div class="positions" role="group" aria-label="当前仓位">
        <button
          v-for="n in 11"
          :key="n"
          :aria-pressed="position === (n - 1) * 10"
          @click="choose((n - 1) * 10)"
        >
          {{ (n - 1) * 10 }}%
        </button>
      </div>
      <p class="muted small">
        仓位指黄金市值占“黄金市值＋为黄金投资预留的现金”的比例。
      </p>
    </div>
    <FeedState />
    <details
      v-if="report?.recommendations.length"
      class="card overview-details"
    >
      <summary>11档仓位推荐总览</summary>
      <p class="muted small">
        当前、目标以%计；调整以百分点计。同一轮参考，点击仓位切换详情。
      </p>
      <div class="overview-scroll">
        <table class="position-overview">
          <thead>
            <tr>
              <th>当前</th>
              <th>操作</th>
              <th>目标</th>
              <th>调整</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in report.recommendations"
              :key="row.position_pct"
              :class="{ selected: position === row.position_pct }"
            >
              <td>
                <button
                  @click="choose(row.position_pct)"
                  :aria-label="`查看${row.position_pct}%仓位推荐`"
                >
                  {{ row.position_pct }}%
                </button>
              </td>
              <td>{{ actions[row.action] }}</td>
              <td>{{ row.target_position_pct }}%</td>
              <td>
                {{ row.target_position_pct - row.position_pct > 0 ? "+" : ""
                }}{{ row.target_position_pct - row.position_pct }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
    <template v-if="recommendation">
      <section
        class="card recommendation"
        :class="{ outdated: reportStatus !== 'current' }"
      >
        <div class="card-kicker">
          <span>这档仓位的操作参考</span
          ><span>{{
            reportStatus === "current" ? "当前参考" : "历史方案 · 待复核"
          }}</span>
        </div>
        <h2 class="action-title">{{ actions[recommendation.action] }}</h2>
        <div class="target-flow">
          <div>
            <span>当前仓位</span><strong>{{ position }}<small>%</small></strong>
          </div>
          <div class="flow-arrow">→</div>
          <div>
            <span>目标仓位</span
            ><strong
              >{{ recommendation.target_position_pct }}<small>%</small></strong
            >
          </div>
        </div>
        <p>{{ recommendation.reason }}</p>
        <div class="action-note">
          {{
            difference > 0
              ? `增加${difference}个百分点`
              : difference < 0
                ? `降低${-difference}个百分点`
                : "本轮维持当前比例"
          }}
          · 数日至两周波段
        </div>
        <div v-if="reportStatus !== 'current'" class="muted small">
          方案待复核，请先查看最新报价及下方复评条件。
        </div>
      </section>
      <section v-if="basis" class="card">
        <h2>为什么这样安排</h2>
        <p>{{ basis.position_rationale }}</p>
        <details v-if="basis.entry_scenario" class="details">
          <summary>假设入场情景与扣费比较</summary>
          <div class="scenario-grid">
            <div>
              <span>假设入场</span
              ><strong>{{ fmt(basis.entry_scenario.entry_price_cny) }}</strong>
            </div>
            <div>
              <span>目标参考</span
              ><strong>{{ fmt(basis.entry_scenario.target_price_cny) }}</strong>
            </div>
            <div>
              <span>退出参考</span
              ><strong>{{ fmt(basis.entry_scenario.exit_price_cny) }}</strong>
            </div>
          </div>
          <p class="muted small">{{ basis.entry_scenario.basis }}</p>
          <div v-if="metrics" class="notice">
            <div>
              扣费目标收益 {{ fmt(metrics.net_upside_pct) }}% · 退出收益
              {{ fmt(metrics.net_downside_pct) }}%
            </div>
            <div>按目标仓位配置的退出情景影响约 {{ fmt(weightedRisk) }}%</div>
            <div v-if="metrics.reward_risk_ratio != null">
              收益风险比 {{ fmt(metrics.reward_risk_ratio) }}
            </div>
            <div class="muted small">
              卖费参考
              {{
                fmt(metrics.sell_fee_rate * 100)
              }}%。假设新建仓测算，不代表已有持仓盈亏，也不代表建议买入。
            </div>
          </div>
        </details>
        <div v-else-if="basis.no_entry_reason" class="notice muted small">
          {{ basis.no_entry_reason }}
        </div>
      </section>
      <section class="card">
        <h2>条件方案</h2>
        <template v-if="recommendation.conditions.length"
          ><div
            v-for="(condition, i) in recommendation.conditions"
            :key="i"
            class="condition-item"
          >
            <span class="condition-index">{{ i + 1 }}</span>
            <div>
              京东参考价 {{ condition.condition === "at_or_above" ? "≥" : "≤" }}
              <strong>{{ fmt(condition.price_cny) }} 元/克</strong>
              <p>
                {{ actions[condition.action] }}至
                {{ condition.target_position_pct }}%
              </p>
            </div>
          </div></template
        >
        <p v-else>
          {{ basis?.no_conditions_reason || "本档本轮未推荐条件方案。" }}
        </p>
      </section>
      <section class="card">
        <h2>什么情况下重新评估</h2>
        <Review v-if="basis" :triggers="basis.review_triggers" />
        <p
          v-if="!basis || !recommendation.invalidation.includes('共用')"
          class="muted"
        >
          {{ recommendation.invalidation }}
        </p>
        <div class="muted small">
          本轮生成 {{ time(report.created_at) }} · 参考有效至
          {{ time(report.expires_at) }}
        </div>
      </section>
    </template>
  </div>
</template>
