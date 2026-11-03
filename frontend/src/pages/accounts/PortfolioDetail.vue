<script setup>
import { computed, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
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
            <div class="l">持仓浮盈亏</div>
            <div class="v">{{ money(a.unrealized_pnl, true) }}</div>
          </div>
          <div>
            <div class="l">已实现盈亏</div>
            <div class="v">{{ money(a.realized_pnl, true) }}</div>
          </div>
          <div>
            <div class="l">累计手续费</div>
            <div class="v">{{ money(a.fees) }}</div>
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
          <details v-if="a.plan.decision.invalidation" class="more">
            <summary>
              {{
                a.plan.decision.action === "wait"
                  ? "买回观察与复查条件"
                  : "计划失效条件"
              }}
            </summary>
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
