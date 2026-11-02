<script setup>
import {
  accountName,
  actions,
  statusLabel,
  planStatus,
  validity,
  executionRange,
  num,
  signed,
  fmtTs,
  modelNames,
} from "../format";
defineProps({
  record: Object,
  data: Object,
  kind: { type: String, default: "fills" },
});
</script>
<template>
  <li class="trade-record">
    <div class="row trade-heading">
      <strong class="grow">{{
        accountName(record.account || record.strategy, data)
      }}</strong
      ><span class="tag">{{
        kind === "plans"
          ? (actions[record.decision?.action] || record.decision?.action) +
            " · " +
            statusLabel(record)
          : (record.reason || "").startsWith("实验初始化")
            ? "初始化建仓"
            : record.side === "buy"
              ? "买入已成交"
              : "卖出已成交"
      }}</span>
    </div>
    <template v-if="kind === 'fills'"
      ><div class="trade-amount">
        {{ num(record.grams, 4) }} 克 × {{ num(record.price) }} 元/克
      </div>
      <div class="sub">成交时间 {{ fmtTs(record.stamp || record.ts) }}</div>
      <div class="sub">
        成交金额 {{ num(record.grams * record.price) }} 元 · 手续费
        {{ num(record.fee) }} 元<span
          v-if="record.side === 'sell' && record.pnl != null"
        >
          · 本笔已实现盈亏 {{ signed(record.pnl) }} 元</span
        >
      </div>
      <div class="sub">
        {{ record.reason
        }}<span v-if="(record.reason || '').startsWith('实验初始化')">
          · 用于建立满仓起点，不是 AI 主动买入。</span
        >
      </div>
      <details v-if="record.decision" class="more">
        <summary>查看决策依据</summary>
        <div class="sub">
          模型
          {{ modelNames[record.decision.analyst] || record.decision.analyst }} ·
          判断 #{{ record.decision.view?._view_id || "—" }} · 轮次
          {{ record.decision.view?._run_id || "—" }}
        </div>
        <p class="sub">{{ record.decision.view?.summary || record.reason }}</p>
        <div v-if="record.decision.execution_quote" class="sub">
          成交报价：京东接口
          {{ num(record.decision.execution_quote.price) }} 元/克 ·
          {{ fmtTs(record.decision.execution_quote.quote_at) }}
        </div>
      </details></template
    ><template v-else
      ><div class="trade-amount">
        {{
          record.decision?.action === "buy"
            ? "计划买入 " + num(record.decision.amount_cny) + " 元"
            : record.decision?.action === "sell"
              ? "计划卖出 " + num(record.decision.grams, 4) + " 克"
              : "不产生买卖成交"
        }}
      </div>
      <div class="sub">
        决策时间 {{ fmtTs(record.stamp || record.created_at) }}
      </div>
      <div v-if="['buy', 'sell'].includes(record.decision?.action)" class="sub">
        {{
          record.decision.execution === "limit"
            ? "委托限价 " + num(record.decision.limit_price_cny) + " 元/克"
            : "市价委托，以实际成交报价为准"
        }}
        · {{ executionRange(record.decision) }}
      </div>
      <div class="sub">{{ validity(record) }}</div>
      <div>{{ record.decision?.reason || "未提供决策理由" }}</div>
      <div v-if="planStatus(record) !== 'pending' && record.reason" class="sub">
        处理结果：{{ record.reason }}
      </div>
      <details class="more">
        <summary>保护设置与决策详情</summary>
        <div class="sub">
          止损
          {{
            record.decision?.clear_stop_loss
              ? "移除"
              : record.decision?.stop_loss_cny == null
                ? "保留原设置"
                : num(record.decision.stop_loss_cny) + " 元/克"
          }}
          · 止盈
          {{
            record.decision?.clear_take_profit
              ? "移除"
              : record.decision?.take_profit_cny == null
                ? "保留原设置"
                : num(record.decision.take_profit_cny) + " 元/克"
          }}
        </div>
        <div class="sub">
          判断 #{{ record.view_id }} · 计划 #{{ record.id }}
        </div>
        <div v-if="record.decision?.invalidation" class="sub">
          失效条件：{{ record.decision.invalidation }}
        </div>
      </details></template
    >
  </li>
</template>
