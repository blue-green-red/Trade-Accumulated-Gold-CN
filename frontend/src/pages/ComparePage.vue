<script setup>
import { computed, ref } from "vue";
import Card from "../components/Card.vue";
import Chart from "../components/Chart.vue";
import PnlBreakdown from "../components/PnlBreakdown.vue";
import {
  num,
  pct,
  rate,
  fmtTs,
  accountLabels,
  difference,
  riskMode,
} from "../format";
import { navigate, selectView } from "../composables/dashboard";
const risk = ref("all");
const props = defineProps({ data: Object });
const models = computed(() => props.data.comparison?.models || []);
const rows = computed(() =>
  Object.keys(accountLabels).flatMap((key) =>
    models.value.flatMap((m) =>
      m.accounts
        .filter(
          (a) =>
            a.strategy === key &&
            (risk.value === "all" || key.startsWith(risk.value + "_")),
        )
        .map((a) => ({ ...a, modelName: m.name })),
    ),
  ),
);
const curves = computed(() =>
  models.value.map((m, i) => ({
    key: m.key,
    name: m.name,
    points: m.points,
    color: `var(--series-${i + 1})`,
  })),
);
const mismatched = computed(
  () =>
    models.value.some((m) => !m.started_at) ||
    new Set(models.value.map((m) => m.started_at)).size > 1,
);
</script>
<template>
  <section id="panel-compare" class="page-stack" data-page-ready="true">
    <button
      class="link-btn back-link"
      @click="selectView('strategies', 'simulation')"
    >
      ‹ 返回模拟账户
    </button>
    <details class="more card">
      <summary>六账户怎样比较</summary>
      <Card
        :title="
          data.trading_mode === 'four_account'
            ? '四账户怎样比较'
            : '六账户怎样比较'
        "
        subtitle="按同模型、同风险及相同起点比较"
        ><div class="grid-2 benchmark-guide">
          <div class="mini-stat">
            <strong>初始满仓 → 满仓持有不动</strong>
            <div class="sub">
              假设开账时买入后一直不交易，按当前金价扣除卖出费用估值。用来判断
              AI 买卖是否优于一直持有。
            </div>
          </div>
          <div class="mini-stat">
            <strong>初始空仓 → 空仓持币不动</strong>
            <div class="sub">
              假设一直保留初始本金，基准收益为 0%。用来判断 AI
              入场交易是否创造收益。
            </div>
          </div>
        </div>
        <p class="sub">
          {{
            riskMode(data) ? "低、标准、高风险各有以上两个账户。" : ""
          }}基准按每户相同起点计算，不额外开户。AI 增益 = AI 收益率 −
          基准收益率，例如 AI +3%、基准 +2%，领先 1 个百分点。
        </p></Card
      >
    </details>
    <div v-if="mismatched" class="notice warn">
      实验起点不同或有模型尚未开始，请结合运行时间、同起点基准与平仓样本比较，不按收益直接排名。
    </div>
    <div class="grid-2">
      <Card
        v-for="m in models"
        :key="m.key"
        :title="m.name"
        :subtitle="`六户合计 · 起始 ${fmtTs(m.started_at)}`"
        ><div class="mini-stats">
          <div class="mini-stat">
            <div class="l">总收益率</div>
            <div class="v">{{ pct(m.return) }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">交易胜率</div>
            <div class="v">{{ rate(m.win_rate) }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">最大回撤</div>
            <div class="v">{{ pct(m.max_drawdown) }}</div>
          </div>
        </div>
        <p class="sub">
          本金 {{ num(m.budget, 0) }} 元 · 净值
          {{ num(m.value, 0) }} 元<br />不操作的组合基准
          {{ pct(m.baseline_return) }} · {{ difference(m.excess_return)
          }}<br />完整平仓 {{ m.trades }} 次 · 盈利 {{ m.wins }} / 亏损
          {{ m.losses }} · 盈利因子 {{ num(m.profit_factor) }}
        </p>
        <div v-if="m.small_sample" class="sub">
          样本积累中 · 满 30 次平仓后再评价胜率
        </div>
        <details v-if="m.pnl_breakdown" class="more">
          <summary>查看盈亏归因</summary>
          <PnlBreakdown :value="m.pnl_breakdown" /></details
      ></Card>
    </div>
    <Card v-if="!models.length" title="模型成绩"
      ><div class="empty">暂无已开启模型，请在 AI 页开启模型后查看。</div></Card
    ><Card :title="riskMode(data) ? '六账户收益与基准' : '同类策略对比'"
      ><div class="chips">
        <button
          v-for="[key, label] in [
            ['all', '全部风险'],
            ['low', '低风险'],
            ['standard', '标准风险'],
            ['high', '高风险'],
          ]"
          :key="key"
          class="filter-btn"
          :aria-pressed="risk === key"
          @click="risk = key"
        >
          {{ label }}
        </button>
      </div>
      <p class="sub">
        风险筛选仅影响下方账户明细；上方模型成绩与组合曲线始终包含六户。
      </p>
      <div class="table-wrap compare-desktop">
        <table>
          <thead>
            <tr>
              <th>风险 / 起点</th>
              <th>模型</th>
              <th>AI 收益率</th>
              <th>不操作的基准收益</th>
              <th>相比基准（百分点）</th>
              <th>回撤</th>
              <th>胜率</th>
              <th>平仓次数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="a in rows" :key="a.key">
              <td>{{ accountLabels[a.strategy] || a.name }}</td>
              <td>{{ a.modelName }}</td>
              <td>{{ pct(a.return) }}</td>
              <td>
                {{ a.baseline_name }}
                <div class="sub">{{ pct(a.baseline_return) }}</div>
              </td>
              <td>{{ difference(a.excess_return) }}</td>
              <td>{{ pct(a.max_drawdown) }}</td>
              <td>{{ rate(a.win_rate) }}</td>
              <td>{{ a.trades }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="compare-mobile">
        <div
          v-for="a in rows"
          :key="a.key"
          style="padding: 12px 0; border-bottom: 1px solid var(--grid)"
        >
          <strong>{{ accountLabels[a.strategy] || a.name }}</strong>
          <div class="row">
            <strong class="grow">{{ a.modelName }}</strong
            ><strong>{{ pct(a.return) }}</strong>
          </div>
          <div class="sub">
            胜率 {{ rate(a.win_rate) }} · 平仓 {{ a.trades }} 次 · 回撤
            {{ pct(a.max_drawdown) }}
          </div>
          <div class="sub">
            基准：{{ a.baseline_name }} {{ pct(a.baseline_return) }}
          </div>
          <div class="sub">
            AI 收益 {{ pct(a.return) }} · {{ difference(a.excess_return) }}
          </div>
        </div>
      </div></Card
    ><Card
      title="组合收益曲线"
      subtitle="各账户按本金加权，包含未投资现金与持仓浮盈亏"
      ><Chart :series="curves" percent empty-text="新实验数据积累中"
    /></Card>
    <details class="more card">
      <summary>比较口径</summary>
      <p class="sub">
        每户初始10万元；满仓持有、空仓持币基准使用同一起点。胜率按完整平仓周期扣费统计，分批卖出合并。未成交计划不算胜率样本，关闭期间已有资产仍计盈亏。旧概率账户不参与当前实验。
      </p>
    </details>
  </section>
</template>
