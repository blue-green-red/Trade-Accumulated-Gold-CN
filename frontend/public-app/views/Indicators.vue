<script setup>
import { useFeed } from "../useFeed.js";
import { fmt, time } from "../state.js";
const { data, error } = useFeed("/api/public/market?view=indicators", 60000);
</script>
<template>
  <div
    class="view-stack"
    data-public-view="indicators"
    :data-ready="data !== null"
  >
    <div v-if="error" class="notice warn">
      指标暂时无法更新，以下为上次内容。
    </div>
    <section class="card">
      <div class="section-heading">
        <h2>宏观环境</h2>
        <span class="status-pill" :class="{ warn: data?.status !== 'fresh' }">{{
          data?.status === "fresh" ? "已更新" : "参考数据待更新"
        }}</span>
      </div>
      <p class="muted small">报价时间 {{ time(data?.fetched_at) }}</p>
      <div class="indicator-grid">
        <article v-for="q in data?.quotes || []" :key="q.name">
          <span class="muted small">{{ q.name }}</span>
          <div class="indicator-value">
            {{ fmt(q.value, q.digits) }}<small>{{ q.unit }}</small>
          </div>
          <div v-if="q.change_pct != null" class="muted small">
            较前收 {{ q.change_pct > 0 ? "+" : "" }}{{ fmt(q.change_pct) }}%
          </div>
          <p class="muted small">{{ q.note }}</p>
        </article>
      </div>
    </section>
    <section class="card">
      <div class="section-heading">
        <h2>国际黄金日线</h2>
        <span class="status-pill">{{
          data?.technical.status === "fresh" ? "已更新" : "历史参考"
        }}</span>
      </div>
      <div class="scenario-grid">
        <div>
          <span>RSI</span><strong>{{ fmt(data?.technical.rsi, 1) }}</strong>
        </div>
        <div>
          <span>MA20</span><strong>{{ fmt(data?.technical.ma20_usd) }}</strong>
        </div>
        <div>
          <span>MA50</span><strong>{{ fmt(data?.technical.ma50_usd) }}</strong>
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
