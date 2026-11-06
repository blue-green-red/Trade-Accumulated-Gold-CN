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
          到该价时，维持现仓的账户净值变动约
          {{ signed(effect.account_change_pct) }}%。
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
