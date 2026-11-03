<script setup>
import { ref, computed, defineAsyncComponent, watch } from "vue";
import { feeds } from "../cache.js";
import { clock, fmt, time } from "../state.js";
import { usePage } from "../usePage.js";
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
      行情暂时无法更新，已有价格仅作历史参考。
    </div>
    <section class="card price-card">
      <div class="card-kicker">
        <span>京东积存金</span><span class="status-pill">{{ quoteState }}</span>
      </div>
      <div class="main-price">
        {{ fmt(quote?.quote.price_cny) }}<span>元 / 克</span>
      </div>
      <div class="muted">
        {{ quote?.quote.source || "京东通用参考报价" }} ·
        {{ time(quote?.quote.quoted_at) }}
      </div>
      <p class="price-note">实际价格与费用以所选产品为准。</p>
      <div class="market-metrics">
        <div>
          <span>国际现货黄金</span
          ><strong
            >{{ fmt(quote?.international.price_usd_oz)
            }}<small>美元/盎司</small></strong
          ><em v-if="quote?.international.change_pct != null"
            >{{ quote.international.change_pct > 0 ? "+" : ""
            }}{{ fmt(quote.international.change_pct) }}%</em
          >
        </div>
        <div>
          <span>美元兑人民币</span
          ><strong>{{ fmt(quote?.fx.usd_cny, 4) }}</strong
          ><small>USD / CNY</small>
        </div>
      </div>
    </section>
    <section class="card">
      <div class="section-heading">
        <div>
          <span class="eyebrow">PRICE HISTORY</span>
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
      <div v-if="chartError" class="notice warn">走势暂时无法更新。</div>
      <div v-if="!history && !chartError" class="empty chart-empty">
        正在读取走势…
      </div>
      <PriceChart v-else :points="history?.points || []" />
      <p class="muted small">
        京东报价本地采样 · 元/克 · 非官方完整K线，休市时段不补点。
      </p>
    </section>
    <div class="session-note" v-if="quote">
      {{ quote.session.rule
      }}<span v-if="!quote.session.is_open">
        · 下次开盘 {{ time(quote.session.next_open) }}</span
      >
    </div>
  </div>
</template>
