<script setup>
import { money } from "../format";
defineProps({ value: Object });
</script>
<template>
  <div v-if="value" class="pnl-breakdown">
    <div class="asset-detail-grid">
      <div>
        <div class="l">价差盈亏 · 未扣费用</div>
        <div class="v">{{ money(value.price_pnl_cny, true) }}</div>
      </div>
      <div>
        <div class="l">已付手续费</div>
        <div class="v">
          {{
            money(
              value.paid_fees_cny == null ? null : -value.paid_fees_cny,
              true,
            )
          }}
        </div>
      </div>
      <div v-if="value.paid_spread_cny">
        <div class="l">已付买卖价差</div>
        <div class="v">{{ money(-value.paid_spread_cny, true) }}</div>
      </div>
      <div>
        <div class="l">预计卖出手续费 · 尚未支付</div>
        <div class="v">
          {{
            money(
              value.estimated_exit_fee_cny == null
                ? null
                : -value.estimated_exit_fee_cny,
              true,
            )
          }}
        </div>
      </div>
      <div v-if="value.estimated_exit_spread_cny">
        <div class="l">预计卖出价差</div>
        <div class="v">{{ money(-value.estimated_exit_spread_cny, true) }}</div>
      </div>
      <div>
        <div class="l">累计净盈亏 · 已计以上费用</div>
        <div class="v">{{ money(value.net_pnl_cny, true) }}</div>
      </div>
    </div>
    <p class="sub">
      价差盈亏减去已付费用和预计卖出费用，得到累计净盈亏。预计费用用于当前持仓的清算估值，尚未实际扣款，不要再次从净值中扣减。
    </p>
  </div>
</template>
