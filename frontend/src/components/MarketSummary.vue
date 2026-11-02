<script setup>
import { computed } from "vue";
import { num, signed, fmtTs, sourceState } from "../format";
const props = defineProps({ data: Object });
const q = computed(
  () =>
    props.data.execution?.quote || props.data.execution?.valuation_quote || {},
);
const daily = computed(
  () =>
    props.data.daily_change || props.data.wave_evidence?.daily_reference || {},
);
</script>
<template>
  <div class="market-quotes" aria-label="实时行情摘要">
    <div class="quote-tile primary">
      <div class="sub">京东积存金</div>
      <div class="quote-value">
        {{ num(q.price) }}<span class="quote-unit">元/克</span>
      </div>
      <div class="data-meta">
        <span class="data-state" :class="{ warn: !data.execution?.ready }">{{
          data.execution?.ready
            ? "可模拟成交"
            : data.snapshot?.jd_session?.is_open === false
              ? "京东休市 · 暂停成交"
              : "报价不可用 · 暂停成交"
        }}</span
        ><span
          >报价 {{ fmtTs(q.quote_at) }} ·
          {{
            data.snapshot?.jd_session?.is_open === false
              ? "最近有效报价"
              : "每分钟更新"
          }}</span
        ><span>交易时段：周一09:00至周六02:30（北京时间）</span
        ><span v-if="data.snapshot?.jd_session?.next_open"
          >下次开盘 {{ fmtTs(data.snapshot.jd_session.next_open) }}</span
        >
      </div>
    </div>
    <div class="quote-tile">
      <div class="sub">国际现货黄金</div>
      <div class="quote-value">
        {{ num(data.snapshot?.xauusd)
        }}<span class="quote-unit">美元/盎司</span>
      </div>
      <div class="sub">日涨跌 {{ signed(data.snapshot?.change_pct) }}%</div>
      <div class="data-meta">
        {{ sourceState(data.snapshot?.ts) }} · {{ fmtTs(data.snapshot?.ts) }}
      </div>
    </div>
    <div class="quote-tile">
      <div class="sub">美元兑人民币</div>
      <div class="quote-value">
        {{ num(data.snapshot?.fx?.rate, 4)
        }}<span class="quote-unit">USD/CNY</span>
      </div>
      <div class="data-meta">
        {{ sourceState(data.snapshot?.fx?.fetched_at, 1200) }} · 每15分钟
      </div>
    </div>
    <div class="quote-tile">
      <div class="sub">期货近5日涨跌</div>
      <div class="quote-value">
        {{ signed(daily.changes_pct?.["5d"]) }}<span class="quote-unit">%</span>
      </div>
      <div class="sub">GC=F代理 · 截止 {{ daily.last_day || "—" }}</div>
    </div>
  </div>
</template>
