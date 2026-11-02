<script setup>
import { computed, defineAsyncComponent, ref, watch } from "vue";
import Card from "../components/Card.vue";
const Chart = defineAsyncComponent(() => import("../components/Chart.vue"));
const LiveAccounts = defineAsyncComponent(
  () => import("../components/LiveAccounts.vue"),
);
import { accountView, navigate } from "../composables/dashboard";
import {
  accountLabels,
  modelChoices,
  periodChoices,
  riskMode,
  modelNames,
  money,
  num,
  pct,
  totals,
  difference,
  statusLabel,
  validity,
  fmtTs,
} from "../format";
const props = defineProps({ data: Object });
const model = ref("all"),
  period = ref("all"),
  start = ref("all");
watch(
  () => props.data.model_controls,
  () => {
    if (
      model.value !== "all" &&
      !props.data.model_controls.some((c) => c.key === model.value && c.enabled)
    )
      model.value = "all";
  },
);
const matches = (key) =>
  (model.value === "all" || key.startsWith(model.value + ":")) &&
  (period.value === "all" || key.includes(":" + period.value + "_")) &&
  (start.value === "all" || key.endsWith("_" + start.value));
const accounts = computed(() =>
  (props.data.leaderboard || [])
    .filter((a) => matches(a.key))
    .sort((a, b) => a.key.localeCompare(b.key)),
);
const summary = computed(() => totals(accounts.value));
const groups = computed(() =>
  Array.from(new Set(accounts.value.map((a) => a.analyst))).map((m) => ({
    key: m,
    name: modelNames[m] || m,
    rows: accounts.value
      .filter((a) => a.analyst === m)
      .sort(
        (a, b) =>
          Object.keys(accountLabels).indexOf(a.start_mode) -
          Object.keys(accountLabels).indexOf(b.start_mode),
      ),
  })),
);
const comparison = computed(
  () =>
    new Map(
      (props.data.comparison?.models || []).flatMap((m) =>
        m.accounts.map((a) => [a.key, a]),
      ),
    ),
);
const curves = computed(() =>
  (props.data.equity?.series || [])
    .filter((s) => matches(s.key))
    .map((s) => ({
      ...s,
      color: `var(--series-${s.key.startsWith("claude:") ? 1 : 2})`,
    })),
);
function records(a, kind) {
  navigate("records", {
    kind,
    model: a.analyst || "all",
    account: a.key,
    days: "all",
  });
}
</script>
<template>
  <section id="panel-strategies" class="page-stack" data-page-ready="true">
    <Card title="账户视图"
      ><div class="chips">
        <button
          v-for="[key, label] in [
            ['live', '真实账户'],
            ['simulation', '模拟账户'],
          ]"
          :key="key"
          class="filter-btn"
          type="button"
          :aria-label="'账户视图：' + label"
          :aria-pressed="data.account_view === key"
          @click="accountView = key"
        >
          {{ label }}
        </button>
      </div>
      <div class="sub">
        {{
          data.account_view === "live"
            ? "银行实际资产与人工执行建议"
            : "每个模型独立模拟账户，资金与真实银行账户分开"
        }}
      </div></Card
    ><LiveAccounts v-if="data.account_view === 'live'" :data="data" /><template
      v-else
      ><Card
        title="账户筛选"
        :subtitle="`${(data.leaderboard || []).length} 个模拟账户`"
        ><div class="filters">
          <label
            >资产模型<select v-model="model" aria-label="资产模型">
              <option v-for="[k, t] in modelChoices(data)" :key="k" :value="k">
                {{ t }}
              </option>
            </select></label
          ><label
            >{{ riskMode(data) ? "风险场景" : "资产周期"
            }}<select
              v-model="period"
              :aria-label="riskMode(data) ? '风险场景' : '资产周期'"
            >
              <option v-for="[k, t] in periodChoices(data)" :key="k" :value="k">
                {{ t }}
              </option>
            </select></label
          ><label
            >资产起点<select v-model="start" aria-label="资产起点">
              <option value="all">全部起点</option>
              <option value="full">初始满仓</option>
              <option value="empty">初始空仓</option>
            </select></label
          >
        </div>
        <p class="sub">
          满仓 /
          空仓指初始状态。满仓账户与一直持有比较，空仓账户与一直持币比较；基准不是额外账户。
        </p></Card
      ><Card
        title="所选账户概览"
        :subtitle="`${summary.count} 个账户 · 已开账 ${summary.started} 个`"
        ><div class="asset-overview">
          <div>
            <div class="sub">净资产</div>
            <div class="v">{{ money(summary.value) }}</div>
          </div>
          <div>
            <div class="sub">累计盈亏</div>
            <div class="v">{{ money(summary.pnl, true) }}</div>
            <div class="sub">收益率 {{ pct(summary.return) }}</div>
          </div>
          <div>
            <div class="sub">可用现金</div>
            <div class="v">{{ money(summary.cash) }}</div>
          </div>
          <div>
            <div class="sub">当前持仓</div>
            <div class="v">{{ summary.held }} / {{ summary.started }} 户</div>
          </div>
        </div>
        <p class="sub">
          已开账本金 {{ money(summary.budget) }} ·
          净资产已计卖出费用，未开账账户不计入汇总。
        </p></Card
      ><Card
        v-for="group in groups"
        :key="group.key"
        class="asset-group"
        :data-model="group.key"
        :title="group.name"
        :subtitle="`${group.rows.length} 个账户 · 已开启`"
        ><p class="sub">
          净资产 {{ money(totals(group.rows).value) }} · 盈亏
          {{ money(totals(group.rows).pnl, true) }}
        </p>
        <details
          v-for="a in group.rows"
          :key="a.key"
          class="strategy asset-account"
          :data-detail-key="a.key"
        >
          <summary>
            <div class="name">
              <span
                class="swatch"
                :style="{
                  background:
                    group.key === 'claude'
                      ? 'var(--series-1)'
                      : 'var(--series-2)',
                }"
              ></span
              ><span class="asset-name">{{
                accountLabels[a.start_mode] || a.name
              }}</span
              ><span class="tag">{{
                !a.started ? "等待开账" : a.grams > 1e-9 ? "持仓中" : "当前空仓"
              }}</span>
            </div>
            <div class="ret">
              {{
                a.started && a.value != null
                  ? money(a.value - a.budget, true)
                  : "—"
              }}
              <div class="sub">{{ pct(a.return) }}</div>
            </div>
            <div class="asset-measures">
              <div>
                <div class="l">净资产</div>
                <div class="v">{{ a.started ? money(a.value) : "未开账" }}</div>
              </div>
              <div>
                <div class="l">可用现金</div>
                <div class="v">{{ a.started ? money(a.cash) : "—" }}</div>
              </div>
              <div>
                <div class="l">当前持仓</div>
                <div class="v">
                  {{ a.started ? num(a.grams, 3) + " 克" : "—" }}
                </div>
              </div>
            </div>
            <div
              v-if="comparison.get(a.key)?.baseline_name"
              class="asset-benchmark"
            >
              对比基准：{{ comparison.get(a.key).baseline_name }}
              {{ pct(comparison.get(a.key).baseline_return) }} · AI 收益
              {{ pct(a.return) }} ·
              {{ difference(comparison.get(a.key).excess_return) }}
            </div>
          </summary>
          <div class="body">
            <div v-if="a.paused" class="notice">
              模型已暂停，已有持仓保护继续执行。
            </div>
            <div
              v-if="a.started && a.grams > 0 && a.stop_price == null"
              class="sub"
            >
              未设置止损：按模型决策保留当前敞口。
            </div>
            <div class="asset-detail-grid">
              <div>
                <div class="l">持仓成本</div>
                <div class="v">
                  {{ a.avg_cost ? num(a.avg_cost) + " 元/克" : "无持仓" }}
                </div>
              </div>
              <div>
                <div class="l">持仓浮动盈亏</div>
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
              <div>
                <div class="l">成交笔数</div>
                <div class="v">{{ a.fills || 0 }} 笔</div>
              </div>
            </div>
            <div class="sub">
              当前保护：止损
              {{
                a.stop_price == null ? "未设置" : num(a.stop_price) + " 元/克"
              }}
              · 止盈
              {{
                a.target_price == null
                  ? "未设置"
                  : num(a.target_price) + " 元/克"
              }}
            </div>
            <div
              v-if="a.risk_budget"
              :class="a.risk_budget.buy_paused ? 'notice warn' : 'sub'"
            >
              单次计划风险 {{ a.risk_budget.trade_risk_pct }}% · 阶段回撤线
              {{ a.risk_budget.drawdown_limit_pct }}% ·
              {{
                a.risk_budget.buy_paused ? "已触线，暂停新增买入" : "预算正常"
              }}
            </div>
            <div v-if="a.plan" class="sub">
              最新决策：{{ statusLabel(a.plan) }} · {{ validity(a.plan) }}
            </div>
            <div class="sub">
              起始 {{ fmtTs(a.started_at) }} · {{ a.description }}
            </div>
            <div class="asset-actions">
              <button class="link-btn" @click="records(a, 'fills')">
                查看成交记录 →</button
              ><button class="link-btn" @click="records(a, 'plans')">
                查看账户决策 →
              </button>
            </div>
          </div>
        </details></Card
      ><Card v-if="!accounts.length" title="账户资产"
        ><div class="empty">当前筛选下暂无账户，可调整上方筛选。</div></Card
      ><Card title="收益率走势" subtitle="随筛选更新 · 满/空代表初始仓位"
        ><Chart
          :series="curves"
          percent
          empty-text="账户刚开始运行，曲线积累几个小时后出现" /></Card
    ></template>
  </section>
</template>
