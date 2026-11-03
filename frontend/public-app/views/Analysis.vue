<script setup>
import { computed } from "vue";
import {
  report,
  loadReport,
  reportLoading,
  names,
  analyst,
  time,
  clock,
} from "../state.js";
import { referenceDeadline } from "../presentation.js";
import { usePage } from "../usePage.js";
import FeedState from "../components/FeedState.vue";
import Review from "../components/Review.vue";
import Assessment from "../components/Assessment.vue";
import KeyLevels from "../components/KeyLevels.vue";
import UpdatePolicy from "../components/UpdatePolicy.vue";
const observations = computed(() =>
  (report.value?.decision_basis?.review_triggers || []).filter(
    (trigger) => trigger.kind !== "price",
  ),
);
usePage(loadReport);
</script>
<template>
  <div
    class="view-stack"
    data-public-view="analysis"
    :data-ready="report !== null"
    :aria-busy="reportLoading"
  >
    <FeedState />
    <template v-if="report?.analysis">
      <section class="card analysis-lead">
        <div class="card-kicker">
          <span>{{ names[analyst] }} · 本轮观点</span
          ><span class="risk-label">高风险 · 数日至两周</span>
        </div>
        <p class="lead-text">{{ report.analysis.outlook }}</p>
        <Assessment :value="report.market_assessment" />
        <div class="change-note">
          <span>本轮变化</span>{{ report.analysis.change_summary }}
        </div>
      </section>
      <section class="card analysis-levels">
        <h2>关注价位</h2>
        <KeyLevels :levels="report.key_levels || []" />
      </section>
      <section class="card analysis-drivers">
        <div class="section-heading">
          <div>
            <h2>影响市场的因素</h2>
          </div>
        </div>
        <ol class="drivers">
          <li v-for="(driver, i) in report.analysis.drivers" :key="i">
            <span>{{ String(i + 1).padStart(2, "0") }}</span>
            <p>{{ driver }}</p>
          </li>
        </ol>
      </section>
      <section class="card analysis-risk">
        <h2>本轮风险</h2>
        <p>{{ report.analysis.risk }}</p>
        <details
          v-if="
            observations.length ||
            (!report.decision_basis && report.analysis.watch_conditions?.length)
          "
          class="details"
        >
          <summary>本轮其他观察点</summary>
          <p class="muted small">
            以下是AI建议关注的事件或时间，用于复核本轮观点，不是系统自动分析的安排。价格观察点见上方“关注价位”。
          </p>
          <Review v-if="report.decision_basis" :triggers="observations" />
          <ul v-else class="review-list">
            <li v-for="item in report.analysis.watch_conditions" :key="item">
              {{ item }}
            </li>
          </ul>
        </details>
        <div class="muted small">
          行情快照 {{ time(report.snapshot_at) }} ·
          {{ referenceDeadline(report.expires_at, clock) }}
          {{ time(report.expires_at) }}
        </div>
        <details v-if="report.data_sources?.length" class="details">
          <summary>本轮公共数据来源</summary>
          <ul class="review-list">
            <li v-for="source in report.data_sources" :key="source.name">
              {{ source.name }} · {{ time(source.as_of) }}
            </li>
          </ul>
        </details>
      </section>
      <section class="card analysis-update">
        <h2>分析更新方式</h2>
        <UpdatePolicy
          :policy="report.update_policy"
          :validity="report.reference_validity"
        />
      </section>
      <a class="text-link" href="#recommendations"
        >根据当前仓位查看操作参考 <span>→</span></a
      >
    </template>
  </div>
</template>
