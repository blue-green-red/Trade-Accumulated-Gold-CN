<script setup>
import { ref, computed, defineAsyncComponent, watch } from "vue";
import { feeds } from "../cache.js";
import { clock, fmt, time } from "../state.js";
import { usePage } from "../usePage.js";
import { freshness, dataLabels } from "../presentation.js";
const internationalStatus = computed(() =>
  freshness(
    quote.value?.international.status,
    quote.value?.international.as_of,
    180,
    clock.value,
  ),
);
const PriceChart = defineAsyncComponent(
  () => import("../components/PriceChart.vue"),
);
const quote = ref(null),
  history = ref(null),
  error = ref(false),
  chartError = ref(false),
  loading = ref(false),
  range = ref("7d");
const quoteState = computed(() => {
  if (!quote.value) return "等待报价";
  if (error.value) return "更新暂失败 · 上次报价";
  if (
    !quote.value.session.is_open ||
    (quote.value.session.close_at &&
      Date.parse(quote.value.session.close_at) <= clock.value)
  )
    return "当前休市";
  const q = quote.value.quote;
  if (!q.valid_until || clock.value >= Date.parse(q.valid_until))
    return q.price_cny == null ? "报价待更新" : "历史参考价";
  return q.status === "cached" ? "近期有效报价" : "报价已更新";
});
let chartGeneration = 0;
async function chart(force = false) {
  const version = ++chartGeneration,
    key = range.value;
  history.value =
    feeds.peek(`/api/public/market?view=history&range=${key}`) || null;
  chartError.value = false;
  try {
    const data = await feeds.read(
      `/api/public/market?view=history&range=${key}`,
      { ttl: 60000, force },
    );
    if (version === chartGeneration) history.value = data;
  } catch {
    if (version === chartGeneration) chartError.value = true;
  }
}
async function load(force = false) {
  loading.value = true;
  error.value = false;
  await Promise.allSettled([
    (async () => {
      try {
        quote.value = await feeds.read("/api/public/market?view=quote", {
          force,
        });
      } catch {
        error.value = true;
      } finally {
        loading.value = false;
      }
    })(),
    chart(force),
  ]);
}
watch(range, () => chart());
usePage(load);
</script>
<template>
  <div
    class="view-stack"
    data-public-view="market"
    :data-ready="Boolean(quote)"
  >
    <div v-if="error" class="notice warn">
      {{
        quote
          ? "行情更新失败，显示上次保存的价格。"
          : "暂时无法读取行情，请稍后刷新。"
      }}
    </div>
    <section class="card price-card">
      <div class="card-kicker">
        <span>京东积存金参考价</span
        ><span class="status-pill">{{ quoteState }}</span>
      </div>
      <div class="main-price">
        {{ fmt(quote?.quote.price_cny) }}<span>元 / 克</span>
      </div>
      <div class="muted">报价时间 {{ time(quote?.quote.quoted_at) }}</div>
      <p class="price-note">京东通用参考报价，产品价格与费用以交易页面为准。</p>
      <div class="market-metrics">
        <div>
          <span>国际现货黄金</span
          ><strong
            >{{ fmt(quote?.international.price_usd_oz)
            }}<small>美元/盎司</small></strong
          ><em v-if="quote?.international.change_pct != null"
            >报价涨跌 {{ quote.international.change_pct > 0 ? "+" : ""
            }}{{ fmt(quote.international.change_pct) }}%</em
          >
          <small
            >{{ dataLabels[internationalStatus] }} ·
            {{ time(quote?.international.as_of) }} 采集</small
          >
        </div>
        <div>
          <span>美元兑人民币</span
          ><strong>{{ fmt(quote?.fx.usd_cny, 4) }}</strong
          ><small>USD / CNY · 日更参考汇率</small><small>非实时即期</small>
          <small v-if="quote?.fx.rate_date"
            >数据日期 {{ quote.fx.rate_date }}</small
          >
          <small v-if="quote?.fx.fetched_at"
            >{{ time(quote.fx.fetched_at) }} 获取</small
          >
        </div>
      </div>
    </section>
    <section class="card">
      <div class="section-heading">
        <div>
          <h2>积存金走势</h2>
        </div>
        <div class="segmented" aria-label="行情范围">
          <button
            v-for="[key, label] in [
              ['1d', '1天'],
              ['7d', '7天'],
              ['30d', '30天'],
            ]"
            :key="key"
            :aria-pressed="range === key"
            @click="range = key"
          >
            {{ label }}
          </button>
        </div>
      </div>
      <div v-if="chartError" class="notice warn">
        {{
          history
            ? "走势更新失败，显示上次记录。"
            : "暂时无法读取走势，请稍后刷新。"
        }}
      </div>
      <div v-if="!history && !chartError" class="empty chart-empty">
        正在读取走势…
      </div>
      <PriceChart v-else :points="history?.points || []" />
      <p class="muted small">京东报价记录 · 元/克 · 仅展示有报价的时段。</p>
    </section>
    <div class="session-note" v-if="quote">
      {{ quote.session.rule
      }}<span v-if="!quote.session.is_open">
        · 下次开盘 {{ time(quote.session.next_open) }}</span
      >
    </div>
  </div>
</template>
