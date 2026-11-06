<script setup>
import { onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
import LiveSync from "../../components/LiveSync.vue";
import { openBank } from "../../composables/dashboard";
import {
  money,
  quantity,
  fmtTs,
  actions,
  modelNames,
  pastDeadline,
} from "../../format";
defineProps({ data: Object });
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
const states = {
  fresh: "已同步",
  stale: "账户快照待更新",
  login_required: "需要京东登录",
  unavailable: "查询失败",
  not_checked: "待首次同步",
};
const adviceStatus = (a, v) =>
  pastDeadline(v)
    ? "建议已到期"
    : !a.connected
      ? "账户数据待更新"
      : {
          current: "人工执行参考",
          review_required:
            v.action_status === "current" ? "部分建议需核对" : "建议需核对",
          account_changed: "账户变化，待复评",
          expired: "建议已到期",
        }[v.status] || "待更新";
</script>
<template>
  <div class="page-stack">
    <LiveSync :data="data" />
    <div
      :class="(data.live_readonly || []).length > 1 ? 'grid-2' : 'page-stack'"
    >
      <Card
        v-for="a in data.live_readonly || []"
        :key="a.bank"
        :title="a.name"
        :subtitle="`${states[a.status] || '待同步'} · ${fmtTs(a.fetched_at)}`"
      >
        <div v-if="!a.connected" class="notice warn">
          {{
            a.fetched_at
              ? "当前保留上次快照，等待恢复采集。"
              : "尚未接通，持仓与资金未知。"
          }}
        </div>
        <div class="mini-stats">
          <div class="mini-stat">
            <div class="l">黄金持仓</div>
            <div class="v">{{ quantity(a.total_grams) }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">
              {{
                a.cash_source === "user_declared_full_position"
                  ? "可用现金 · 用户声明"
                  : "电子户可用现金"
              }}
            </div>
            <div class="v">{{ money(a.cash_cny) }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">浮动盈亏估算 · 未扣卖费</div>
            <div class="v">{{ money(a.holding_pnl_cny, true) }}</div>
          </div>
        </div>
        <div class="sub">
          可卖 {{ quantity(a.sellable_grams) }} · 冻结
          {{ quantity(a.frozen_grams) }}
        </div>
        <div class="live-quote">
          <strong
            >已挂条件单
            {{
              a.pending_order_count == null
                ? "未知"
                : a.pending_order_count + " 笔"
            }}</strong
          >
          <div class="sub">已提交京东的条件单，与 AI 尚未执行的方案分开。</div>
        </div>
        <div v-for="v in a.advice_summary || []" :key="v.id" class="live-quote">
          <strong
            >{{ modelNames[v.analyst] }} · {{ actions[v.action] }}建议</strong
          >
          <div class="sub">
            {{ adviceStatus(a, v) }} · {{ fmtTs(v.created_at) }}
          </div>
          <div class="sub">
            本轮新增条件单方案
            {{ pastDeadline(v) ? 0 : v.suggested_order_count }} 笔 · 需你确认
          </div>
        </div>
        <div v-if="!a.advice_summary?.length" class="sub">
          尚未生成真实建议，等待已开启模型分析。
        </div>
        <div class="detail-actions">
          <button class="chip-btn" @click="openBank(a.bank, 'advice')">
            AI建议 →</button
          ><button class="chip-btn" @click="openBank(a.bank, 'orders')">
            条件单 →</button
          ><button class="link-btn" @click="openBank(a.bank)">持仓详情 →</button
          ><button class="link-btn" @click="openBank(a.bank, 'records')">
            记录 →
          </button>
        </div>
      </Card>
    </div>
  </div>
</template>
