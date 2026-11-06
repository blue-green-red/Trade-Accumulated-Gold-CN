<script setup>
import { num, quantity, money, signed, fmtTs, pastDeadline } from "../format";
defineProps({ items: Array, active: { type: Boolean, default: true } });
const decisions = {
  recommend: "建议提前挂出",
  not_worthwhile: "候选机会不值得参与",
  needs_review: "仍需复评，不建议提前挂出",
  unavailable: "关键信息不足",
};
</script>
<template>
  <section v-if="items?.length" class="live-quote" data-limit-buy-review>
    <strong>低位限价买入评估</strong>
    <p class="sub">候选方案不等于已挂单；新增退出参考不会自动设置持仓止损。</p>
    <div v-for="(item, i) in items" :key="i" class="live-quote">
      <strong>{{
        item.checks?.length
          ? "候选需核对"
          : !active || pastDeadline(item)
            ? "历史候选 · 需重新核对"
            : decisions[item.decision] || "待核对"
      }}</strong>
      <div class="live-advice-values">
        <div>
          <div class="l">候选入场 ≤</div>
          <div class="v">{{ num(item.entry_price_cny) }} 元/克</div>
        </div>
        <div>
          <div class="l">情景目标</div>
          <div class="v">{{ num(item.target_price_cny) }} 元/克</div>
        </div>
        <div>
          <div class="l">新增退出参考</div>
          <div class="v">{{ num(item.risk_exit_cny) }} 元/克</div>
        </div>
        <div>
          <div class="l">候选数量</div>
          <div class="v">{{ quantity(item.grams) }}</div>
        </div>
      </div>
      <div class="sub">
        候选截止 {{ fmtTs(item.expires_at) }} · 北京时间；未知参数保留空缺。
      </div>
      <p class="live-advice-reason">{{ item.basis }}</p>
      <p class="live-advice-reason">{{ item.reason }}</p>
      <div v-if="item.metrics" class="sub">
        <div>
          新增扣费目标收益 {{ signed(item.metrics.net_upside_pct) }}% · 退出收益
          {{ signed(item.metrics.net_exit_return_pct) }}%
        </div>
        <div v-if="item.metrics.budget_cny != null">
          候选金额 {{ money(item.metrics.budget_cny) }}
        </div>
        <div v-if="item.metrics.position_after_pct != null">
          独立买入后黄金仓位约 {{ num(item.metrics.position_after_pct, 1) }}%
        </div>
        <div v-if="item.metrics.account_at_exit_change_pct != null">
          假设买入后金价到达退出参考，账户估值变化约
          {{ signed(item.metrics.account_at_exit_change_pct) }}%。
        </div>
        <div>
          一致扣卖费口径，未计跳价；这是情景测算，不代表一定成交或盈利。
        </div>
      </div>
      <p
        v-for="check in item.checks || []"
        :key="check"
        class="live-advice-check"
      >
        需核对：{{ check }}
      </p>
    </div>
  </section>
</template>
