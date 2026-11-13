<script setup>
import { computed, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
import PnlBreakdown from "../../components/PnlBreakdown.vue";
import PositionPath from "../../components/PositionPath.vue";
import { selectView, navigate } from "../../composables/dashboard";
import {
  accountLabels,
  modelNames,
  num,
  money,
  pct,
  fmtTs,
  actions,
  statusLabel,
  validity,
  executionRange,
  pastDeadline,
} from "../../format";
const props = defineProps({ data: Object });
const a = computed(() => props.data.portfolio);
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
const records = (kind) =>
  navigate("records", {
    kind,
    model: a.value.model,
    account: a.value.key,
    days: "all",
  });
</script>
<template>
  <div class="page-stack">
    <button
      class="link-btn back-link"
      @click="selectView('strategies', 'simulation')"
    >
      ‹ 返回模拟账户
    </button>
    <template v-if="a"
      ><Card
        :title="`${modelNames[a.model]} · ${accountLabels[a.start_mode] || a.name}`"
        :subtitle="`开账 ${fmtTs(a.started_at)}`"
      >
        <div class="mini-stats">
          <div class="mini-stat">
            <div class="l">净资产</div>
            <div class="v">{{ a.started ? money(a.value) : "未开账" }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">累计收益</div>
            <div class="v">{{ pct(a.return) }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">可用现金</div>
            <div class="v">{{ money(a.cash) }}</div>
          </div>
        </div>
        <div class="asset-detail-grid">
          <div>
            <div class="l">当前持仓</div>
            <div class="v">
              {{ num(a.grams, 4) }} 克 · {{ num(a.position_pct) + "%" }}
            </div>
          </div>
          <div>
            <div class="l">持仓均价</div>
            <div class="v">
              {{ a.grams > 0 ? num(a.avg_cost) + " 元/克" : "无持仓" }}
            </div>
          </div>
          <div>
            <div class="l">持仓价差浮盈亏 · 未扣费</div>
            <div class="v">
              {{ money(a.pnl_breakdown?.unrealized_price_pnl_cny, true) }}
            </div>
          </div>
          <div>
            <div class="l">已实现盈亏 · 已扣费</div>
            <div class="v">{{ money(a.realized_pnl, true) }}</div>
          </div>
          <div>
            <div class="l">持仓清算浮盈亏 · 预计扣费</div>
            <div class="v">{{ money(a.unrealized_pnl, true) }}</div>
          </div>
          <div>
            <div class="l">最大回撤</div>
            <div class="v">{{ pct(a.max_drawdown) }}</div>
          </div>
        </div>
        <div class="asset-actions">
          <button class="link-btn" @click="records('fills')">
            查看成交记录 →</button
          ><button class="link-btn" @click="records('plans')">
            查看账户决策 →
          </button>
        </div></Card
      >
      <Card v-if="a.pnl_breakdown" title="盈亏拆解"
        ><PnlBreakdown :value="a.pnl_breakdown"
      /></Card>
      <Card title="最新决策与持仓保护"
        ><div class="sub">
          当前保护：止损
          {{ a.stop_price == null ? "未设置" : num(a.stop_price) + " 元/克" }}
          · 止盈
          {{
            a.target_price == null ? "未设置" : num(a.target_price) + " 元/克"
          }}
        </div>
        <div
          v-if="a.risk_budget"
          class="notice"
          :class="a.risk_budget.buy_paused ? 'warn' : 'info'"
          style="margin-top: 8px"
        >
          单次计划风险 {{ a.risk_budget.trade_risk_pct }}%（{{
            num(a.risk_budget.trade_risk_cny)
          }}元） · 阶段回撤线 {{ a.risk_budget.drawdown_limit_pct }}% ·
          已发生最大回撤 {{ num(a.risk_budget.drawdown_pct) }}% ·
          {{ a.risk_budget.buy_paused ? "已触线，暂停新增买入" : "预算正常" }}
        </div>
        <div
          v-if="a.started && a.grams > 0 && a.stop_price == null"
          class="notice warn"
          style="margin-top: 8px"
        >
          未设置止损：程序不会按固定亏损比例自动平仓
        </div>
        <PositionPath
          v-if="a.plan?.position_path"
          :path="a.plan.position_path"
          simulation
          :active="
            ['pending', 'executed'].includes(a.plan.status) &&
            !pastDeadline(a.plan)
          "
        />
        <template v-if="a.plan?.decision"
          ><div v-if="a.plan.decision.risk_exit_cny" class="sub">
            计划风险退出参考
            {{ num(a.plan.decision.risk_exit_cny) }} 元/克（不是自动止损）
          </div>
          <div
            class="notice"
            :class="a.plan.status === 'rejected' ? 'warn' : 'info'"
            style="margin-top: 10px"
          >
            <strong
              >{{ actions[a.plan.decision.action] }} ·
              {{ statusLabel(a.plan) }}</strong
            >
            <p>{{ a.plan.decision.reason }}</p>
            <div class="sub">
              {{
                a.plan.decision.action === "buy"
                  ? "金额 " + num(a.plan.decision.amount_cny) + " 元"
                  : a.plan.decision.action === "sell"
                    ? "数量 " + num(a.plan.decision.grams, 4) + " 克"
                    : ""
              }}
            </div>
            <div
              v-if="['buy', 'sell'].includes(a.plan.decision.action)"
              class="sub"
            >
              {{
                a.plan.decision.execution === "limit"
                  ? "限价 " + num(a.plan.decision.limit_price_cny) + " 元/克"
                  : "最新京东报价执行"
              }}
              · {{ executionRange(a.plan.decision) }}
            </div>
            <div class="sub">{{ validity(a.plan) }}</div>
            <div class="sub">{{ a.plan.reason }}</div>
          </div>
          <div
            v-if="a.short_wait_review"
            class="notice info"
            style="margin-top: 10px"
          >
            <strong>{{
              a.short_wait_review.entry_limit_cny == null
                ? "观望复核"
                : "限价买入等待复核"
            }}</strong>
            <div class="sub">
              等待始于 {{ fmtTs(a.short_wait_review.waiting_since) }} ·
              {{ num(a.short_wait_review.waiting_minutes, 0) }} 分钟<span
                v-if="a.short_wait_review.same_entry_rounds"
              >
                · 同一入场条件连续
                {{ a.short_wait_review.same_entry_rounds }} 轮</span
              >
            </div>
            <div class="sub" v-if="a.short_wait_review.entry_limit_cny != null">
              当前限价 ≤ {{ num(a.short_wait_review.entry_limit_cny) }} 元/克 ·
              入场区间内采样
              {{ a.short_wait_review.entry_condition_samples }} 次
            </div>
            <div class="sub">
              期间有效采样 {{ a.short_wait_review.samples }} 笔 ·
              {{ num(a.short_wait_review.sampled_low_cny) }}—{{
                num(a.short_wait_review.sampled_high_cny)
              }}
              元/克
            </div>
            <div class="sub">{{ a.short_wait_review.note }}</div>
          </div>
          <details v-if="a.plan.decision.invalidation" class="more">
            <summary>价位依据、失效与复评</summary>
            <div class="sub">{{ a.plan.decision.invalidation }}</div>
          </details></template
        >
        <div v-else class="sub">
          {{
            a.started
              ? "等待活动账户首次决策"
              : "模型开启、后端可用且京东报价有效后，统一初始化活动账户"
          }}
        </div></Card
      ></template
    ><Card v-else title="账户不可用"
      ><div class="empty">模型已关闭或账户尚未建立，请返回账户列表。</div></Card
    >
  </div>
</template>
