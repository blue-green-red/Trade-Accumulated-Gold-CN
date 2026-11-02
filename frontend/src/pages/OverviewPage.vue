<script setup>
import { computed } from "vue";
import Card from "../components/Card.vue";
import MarketSummary from "../components/MarketSummary.vue";
import TradeRecord from "../components/TradeRecord.vue";
import { navigate, accountView } from "../composables/dashboard";
import { pct, rate, fmtTs, modelNames, actions, planStatus } from "../format";
const props = defineProps({ data: Object });
const plans = computed(() =>
  (props.data.autonomous?.plans || []).filter(
    (p) => planStatus(p) === "pending",
  ),
);
function live() {
  accountView.value = "live";
  navigate("strategies");
}
function records(kind) {
  navigate("records", { kind, model: "all", account: "all", days: "all" });
}
const activeAdvice = (a) =>
  (a.advice || []).filter(
    (v) =>
      v.status === "current" && new Date(v.expires_at).getTime() > Date.now(),
  );
</script>
<template>
  <section id="panel-overview" class="page-stack" data-page-ready="true">
    <MarketSummary :data="data" /><Card
      v-if="data.live_readonly?.some((a) => a.fetched_at || a.advice?.length)"
      title="真实账户动态"
      ><template #action
        ><button class="link-btn" @click="live">
          查看资产、条件单与建议 →
        </button></template
      >
      <div class="grid-2">
        <div v-for="a in data.live_readonly" :key="a.bank">
          <strong>{{ a.name }}</strong
          ><span class="tag">{{
            a.connected
              ? "已同步"
              : a.status === "login_required"
                ? "需要登录"
                : "数据待更新"
          }}</span>
          <div class="sub">
            京东待成交条件单
            {{
              a.pending_order_count == null
                ? "未知"
                : a.pending_order_count + " 笔"
            }}
            · 同步 {{ fmtTs(a.fetched_at) }}
          </div>
          <div v-for="v in activeAdvice(a)" :key="v.analyst" class="sub">
            {{ modelNames[v.analyst] }}：{{ actions[v.action] }}建议 · 有效至
            {{ fmtTs(v.expires_at) }}
            <span v-if="v.suggested_order_count">
              · 附 {{ v.suggested_order_count }} 笔待确认条件单方案</span
            >
          </div>
          <div v-if="!activeAdvice(a).length" class="sub">
            暂无当前有效建议，请查看账户状态及最新分析。
          </div>
        </div>
      </div>
      <div class="sub" style="margin-top: 10px">
        真实建议由你确认执行；下方操盘记录属于 AI 模拟账户。
      </div></Card
    ><Card title="模型运行与模拟成绩"
      ><template #action
        ><button class="link-btn" @click="navigate('ai')">
          管理模型 →
        </button></template
      >
      <div class="grid-2">
        <div v-for="m in data.comparison?.models || []" :key="m.key">
          <strong>{{ m.name }}</strong
          ><span class="tag">{{
            data.model_controls.find((c) => c.key === m.key)?.runtime?.running
              ? "分析中"
              : "已开启"
          }}</span>
          <div class="mini-stats">
            <div>收益 {{ pct(m.return) }}</div>
            <div>胜率 {{ rate(m.win_rate) }}</div>
            <div>完整平仓 {{ m.trades }}</div>
          </div>
          <div class="sub">起始 {{ fmtTs(m.started_at) }}</div>
          <div class="sub">
            今日调用
            {{
              data.model_controls.find((c) => c.key === m.key)?.runs_today || 0
            }}
            /
            {{
              data.model_controls.find((c) => c.key === m.key)?.daily_cap || 120
            }}
            轮
          </div>
        </div>
      </div>
      <div v-if="!data.comparison?.models?.length" class="empty">
        暂无已开启模型的模拟账户
      </div>
      <button
        class="link-btn"
        style="margin-top: 12px"
        @click="navigate('compare')"
      >
        比较六账户收益与基准 →
      </button></Card
    ><Card title="最新操作与待成交委托"
      ><template #action><span class="tag">AI 模拟盘</span></template>
      <p class="sub">
        这里仅展示模拟账户。成交会改变模拟资产；委托只是计划，持有与观望不会产生买卖。
      </p>
      <section class="trade-section">
        <div class="row">
          <strong class="grow">最近实际成交 · {{ data.fills.length }}</strong
          ><button class="link-btn" @click="records('fills')">
            查看记录 →
          </button>
        </div>
        <ul v-if="data.fills.length" class="list">
          <TradeRecord
            v-for="f in data.fills"
            :key="f.id || f.ts"
            :record="f"
            :data="data"
          />
        </ul>
        <div v-else class="empty">暂无 AI 账户成交</div>
      </section>
      <section class="trade-section">
        <div class="row">
          <strong class="grow">待成交委托 · {{ plans.length }}</strong
          ><button class="link-btn" @click="records('plans')">
            查看记录 →
          </button>
        </div>
        <ul v-if="plans.length" class="list">
          <TradeRecord
            v-for="p in plans"
            :key="p.id"
            :record="p"
            :data="data"
            kind="plans"
          />
        </ul>
        <div v-else class="sub">
          暂无待成交委托，当前没有等待执行的买卖计划。
        </div>
      </section>
      <details class="more">
        <summary>
          最近到期 ·
          {{ data.autonomous?.recent_expired?.length || 0 }} 条（展开查看）
        </summary>
        <p class="sub">
          委托到期后不再成交；持有、观望的参考到期不会卖出持仓。
        </p>
        <ul class="list">
          <TradeRecord
            v-for="p in data.autonomous?.recent_expired || []"
            :key="p.id"
            :record="p"
            :data="data"
            kind="plans"
          />
        </ul></details></Card
    ><Card title="近期重要事件"
      ><template #action
        ><button class="link-btn" @click="navigate('market')">
          行情与日历 →
        </button></template
      >
      <ul v-if="data.hard_events_24h?.length" class="list">
        <li v-for="e in data.hard_events_24h" :key="e.title + e.time_bj">
          {{ e.time_bj }} {{ e.title }}
        </li>
      </ul>
      <div v-else class="sub">未来24小时暂无已识别重大事件</div></Card
    >
  </section>
</template>
