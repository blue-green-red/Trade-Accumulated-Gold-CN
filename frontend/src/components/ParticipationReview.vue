<script setup>
import { computed } from "vue";
import { clock } from "../clock";
import { money, num, fmtTs } from "../format";
const props = defineProps({ review: Object, snapshot: Boolean, maxAgeSeconds: { type: Number, default: 180 } });
const usable = computed(() => {
  if (props.snapshot) return true;
  const age = (clock.value - Date.parse(props.review?.as_of)) / 1000;
  return Number.isFinite(age) && age >= -30 && age <= props.maxAgeSeconds;
});
const messages = {
  data_unavailable: "报价、资金或委托信息待确认，暂不计算参与对照。",
  no_cash: "当前无确认可追加现金，暂不计算参与对照。",
  buy_paused: "账户已暂停新增买入，暂不计算可追加对照。",
  disabled: "模型已关闭。",
};
const signed = (v) => (v > 0 ? "+" : "") + money(v);
</script>
<template>
  <section v-if="review" class="live-quote" data-participation-review>
    <strong>{{ snapshot ? "分析时的参与与等待对照" : "参与与等待对照" }}</strong>
    <p v-if="review.status !== 'ready' || !usable" class="sub">
      {{ review.status === 'ready' ? "对照报价已过期，等待刷新。" : messages[review.status] || "对照数据待更新。" }}
    </p>
    <template v-else>
      <p class="sub">报价观察截至 {{ fmtTs(review.as_of) }}。</p>
      <div v-if="review.waiting" class="sub">
        <p>
          连续未追加 {{ review.waiting.rounds }} 轮 · 始于
          {{ fmtTs(review.waiting.since) }}。
        </p>
        <p>
          按 {{ fmtTs(review.waiting.reference_quote_at) }} 的输入报价
          {{ num(review.waiting.reference_quote_cny) }} 元/克，假设当时追加
          {{ money(review.waiting.reference_amount_cny) }} 元，持有至本次观察的扣费差额为
          <strong>{{ signed(review.waiting.buy_hold_delta_cny) }} 元</strong>。
          {{ review.waiting.buy_hold_delta_cny > 0 ? "正值表示等待错过的对照收益。" : review.waiting.buy_hold_delta_cny < 0 ? "负值表示等待避开的对照亏损。" : "与保留现金相同。" }}
        </p>
        <p v-if="review.waiting.price_only_delta_cny != null" class="sub">
          观察 {{ num(review.waiting.observed_minutes, 0) }} 分钟：价格变化贡献
          {{ signed(review.waiting.price_only_delta_cny) }} 元，退出成本估计
          {{ money(review.waiting.exit_cost_cny) }} 元。短期费用影响不能单独证明波段等待更优。
        </p>
      </div>
      <p v-else class="sub">尚无同一账户状态下连续未追加的有效记录。</p>
      <details class="more">
        <summary>现在参与：同幅涨跌的金额差</summary>
        <p class="sub">
          报价上下各 {{ review.move_pct }}%，相对继续保留现金的扣费差额：
        </p>
        <div class="comparison-table">
          <table>
            <thead><tr><th>追加对照（元）</th><th>上涨</th><th>下跌</th></tr></thead>
            <tbody>
              <tr><td>0</td><td>0</td><td>0</td></tr>
              <tr v-for="(amount, i) in review.reference_amounts_cny" :key="i">
                <td>{{ money(amount) }}</td><td>{{ signed(review.up_delta_cny[i]) }}</td><td>{{ signed(review.down_delta_cny[i]) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>
      <p class="sub">
        对照规模为净资产1%和5%，以确认现金为限；连续未追加用1%对照。
        连续观察最多14天。按当前费率做输入报价的纸上持有比较，不是成交回放，不模拟期间退出或最佳买卖。
        对照规模不是建议仓位，±2%不是交易或复评阈值。
      </p>
    </template>
  </section>
</template>
<style scoped>
.comparison-table { overflow-x: auto; }
table { width: 100%; font-size: 12px; border-collapse: collapse; }
th, td { padding: 8px 4px; text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
</style>
