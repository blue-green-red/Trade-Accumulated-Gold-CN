<script setup>
import { useFeed } from "../useFeed.js";
import { computed } from "vue";
import { clock, fmt, time } from "../state.js";
import { freshness, dataLabels } from "../presentation.js";
const { data, error } = useFeed("/api/public/market?view=indicators", 60000);
const macroStatus = computed(() =>
  freshness(data.value?.status, data.value?.fetched_at, 420, clock.value),
);
const technicalStatus = computed(() =>
  freshness(
    data.value?.technical.status,
    data.value?.technical.fetched_at,
    180,
    clock.value,
  ),
);
</script>
<template>
  <div
    class="view-stack"
    data-public-view="indicators"
    :data-ready="data !== null"
  >
    <div v-if="error" class="notice warn">
      {{
        data
          ? "指标更新失败，显示上次保存的数据。"
          : "暂时无法读取指标，请稍后刷新。"
      }}
    </div>
    <section class="card">
      <div class="section-heading">
        <h2>宏观环境</h2>
        <span class="status-pill" :class="{ warn: macroStatus !== 'fresh' }">{{
          dataLabels[macroStatus]
        }}</span>
      </div>
      <p class="muted small">获取时间 {{ time(data?.fetched_at) }}</p>
      <div class="indicator-grid">
        <article v-for="q in data?.quotes || []" :key="q.name">
          <span class="muted small">{{ q.name }}</span>
          <div class="indicator-value">
            {{ fmt(q.value, q.digits) }}<small>{{ q.unit }}</small>
          </div>
          <div v-if="q.change_pct != null" class="muted small">
            {{ q.unit === "%" ? "收益率相对变动" : "报价涨跌幅" }}
            {{ q.change_pct > 0 ? "+" : "" }}{{ fmt(q.change_pct) }}%
          </div>
          <p class="muted small">{{ q.note }}</p>
        </article>
      </div>
    </section>
    <section class="card">
      <div class="section-heading">
        <h2>国际黄金日线</h2>
        <span
          class="status-pill"
          :class="{ warn: technicalStatus !== 'fresh' }"
          >{{ dataLabels[technicalStatus] }}</span
        >
      </div>
      <div class="scenario-grid">
        <div>
          <span>RSI · 动量</span
          ><strong>{{ fmt(data?.technical.rsi, 1) }}</strong>
        </div>
        <div>
          <span>20日均线</span
          ><strong>{{ fmt(data?.technical.ma20_usd) }}</strong>
        </div>
        <div>
          <span>50日均线</span
          ><strong>{{ fmt(data?.technical.ma50_usd) }}</strong>
        </div>
      </div>
      <p class="muted small">
        RSI观察动量，接近低值不等于已经见底；均线用于观察趋势，不是买卖指令。
      </p>
      <p class="muted small">{{ data?.note }}</p>
      <div class="muted small">
        {{ data?.technical.source }} · {{ time(data?.technical.fetched_at) }}
      </div>
    </section>
  </div>
</template>
