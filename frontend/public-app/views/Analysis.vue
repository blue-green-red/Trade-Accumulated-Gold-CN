<script setup>
import {
  report,
  loadReport,
  reportLoading,
  names,
  analyst,
  time,
} from "../state.js";
import { usePage } from "../usePage.js";
import FeedState from "../components/FeedState.vue";
import Review from "../components/Review.vue";
import Assessment from "../components/Assessment.vue";
import KeyLevels from "../components/KeyLevels.vue";
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
      <section class="card">
        <h2>关注价位</h2>
        <KeyLevels :levels="report.key_levels || []" />
      </section>
      <section class="card">
        <div class="section-heading">
          <div>
            <span class="eyebrow">WHAT MATTERS</span>
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
      <section class="card">
        <h2>风险与下一次观察</h2>
        <p>{{ report.analysis.risk }}</p>
        <Review
          v-if="report.decision_basis"
          :triggers="report.decision_basis.review_triggers"
        />
        <ul v-else class="review-list">
          <li
            v-for="item in report.analysis.watch_conditions || []"
            :key="item"
          >
            {{ item }}
          </li>
        </ul>
        <div class="muted small">
          行情快照 {{ time(report.snapshot_at) }} · 参考有效至
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
      <a class="text-link" href="#recommendations"
        >根据当前仓位查看操作参考 <span>→</span></a
      >
    </template>
  </div>
</template>
