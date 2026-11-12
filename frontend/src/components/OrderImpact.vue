<script setup>
import { num, signed, money } from "../format";
defineProps({ effects: Object, kind: String });
</script>
<template>
  <div v-if="effects" class="live-quote" data-order-impact>
    <template v-for="(effect, leg) in effects" :key="leg">
      <div v-if="effect" class="sub">
        <strong
          >{{
            leg === "stop_loss"
              ? "止损卖出后"
              : kind === "take_profit_stop_loss"
                ? "止盈卖出后"
                : "触发成交后"
          }}黄金仓位约 {{ num(effect.position_after_pct, 1) }}%</strong
        >
        <div>
          相对分析时账户净估值变化 {{ signed(effect.account_change_cny) }} 元
          （{{ signed(effect.account_change_pct) }}%）。
        </div>
        <div v-if="effect.position_pnl_cny != null">
          本笔按买入均价估算盈亏
          {{ signed(effect.position_pnl_cny) }} 元（已扣卖费）。
        </div>
        <div v-else-if="kind !== 'limit_buy'">
          买入成本未知，本笔盈亏暂无法估算。
        </div>
        <div v-if="effect.sale_fee_cny > 0">
          本笔卖费约 {{ money(effect.sale_fee_cny) }}。
        </div>
      </div>
    </template>
    <div class="sub">
      各单独立估算，不叠加其他委托成交；净值采用一致扣卖费口径，未计跳价。
    </div>
  </div>
</template>
