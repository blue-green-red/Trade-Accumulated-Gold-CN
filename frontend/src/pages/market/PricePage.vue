<script setup>
import { computed, ref, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
import { num, signed, fmtTs, executionStatus } from "../../format";
const props = defineProps({ data: Object });

import Chart from "../../components/Chart.vue";
import { navigate, selectView } from "../../composables/dashboard";
const range = ref("daily");
const quote = computed(
  () =>
    props.data.execution?.quote || props.data.execution?.valuation_quote || {},
);
const quoteState = computed(() => executionStatus(props.data));
const daily = computed(() => props.data.wave_evidence?.jd_daily || {});
const points = computed(() =>
  range.value === "daily"
    ? (daily.value.days || [])
        .filter((r) => Number.isFinite(r.last_price_cny))
        .map((r) => [r.date_bj, r.last_price_cny])
    : props.data.prices?.[range.value] || [],
);
function live() {
  selectView("strategies", "live");
}

const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div
    id="market-prices"
    class="page-stack"
    data-market-view="prices"
    role="tabpanel"
    aria-label="金价走势"
  >
    <Card title="京东积存金" subtitle="元/克">
      <div class="market-main-price">
        {{ num(quote.price) }}<span class="quote-unit">元/克</span>
      </div>
      <div class="data-meta">
        <span
          class="data-state"
          :class="{ warn: !quoteState.ready || quoteState.cached }"
          >{{ quoteState.label }}</span
        ><span>报价 {{ fmtTs(quote.quote_at) }}</span>
        <span v-if="quoteState.note">{{ quoteState.note }}</span>
      </div>
      <div class="price-aux">
        <div>
          <span class="sub">国际现货黄金</span>
          <div>
            <strong>{{ num(data.snapshot?.xauusd) }}</strong>
            <span class="sub"
              >美元/盎司 · {{ signed(data.snapshot?.change_pct) }}%</span
            >
          </div>
        </div>
        <div>
          <span class="sub">美元兑人民币</span>
          <div>
            <strong>{{ num(data.snapshot?.fx?.rate, 4) }}</strong>
            <span class="sub">USD/CNY</span>
          </div>
        </div>
      </div>
      <button class="link-btn" style="margin-top: 12px" @click="live">
        查看浙商、民生真实账户报价 →
      </button>
    </Card>
    <Card title="价格走势">
      <div class="seg market-range" role="group" aria-label="走势范围">
        <button
          v-for="[key, label] in [
            ['daily', '京东逐日'],
            ['1d', '国际分时·1天'],
            ['7d', '国际分时·7天'],
          ]"
          :key="key"
          :aria-pressed="range === key"
          @click="range = key"
        >
          {{ label }}
        </button>
      </div>
      <Chart
        :unit="range === 'daily' ? '元/克' : '美元/盎司'"
        :series="[
          {
            key: range,
            name: range === 'daily' ? '京东逐日金价' : '国际现货黄金',
            points,
          },
        ]"
        empty-text="该范围内暂无有效价格数据"
      />
      <p class="sub">
        {{
          range === "daily"
            ? "京东报价本地采样 · 元/克 · 每个有报价日期的末笔价格，非官方收盘价。"
            : "国际现货黄金 · 美元/盎司 · 本地行情采样。"
        }}
      </p>
    </Card>
  </div>
</template>
