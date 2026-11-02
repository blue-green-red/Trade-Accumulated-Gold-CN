<script setup>
import { computed, ref, watch, onUnmounted } from "vue";
import Card from "../components/Card.vue";
import TradeRecord from "../components/TradeRecord.vue";
import {
  historyFilters as filter,
  currentPage,
  requestJSON,
  navigate,
  accountView,
} from "../composables/dashboard";
import {
  modelChoices,
  accountName,
  fmtTs,
  num,
  pct,
  bias,
  displayTitle,
} from "../format";
const props = defineProps({ data: Object });
const rows = ref([]),
  more = ref(false),
  busy = ref(false),
  error = ref("");
const visibleRows = computed(() => {
  const hidden = (props.data.model_controls || [])
    .filter((c) => !c.enabled)
    .map((c) => c.key);
  return rows.value.filter((r) => {
    const owner = (r.account || r.strategy || "").split(":")[0];
    return (
      !hidden.includes(owner) &&
      !hidden.some((m) => (r.title || "").toLowerCase().includes(m))
    );
  });
});
const kinds = [
  ["fills", "实际成交"],
  ["plans", "AI 决策"],
  ["cycles", "交易周期"],
  ["analysis", "分析历史"],
  ["alerts", "系统提醒"],
];
const descriptions = {
  fills:
    "AI 模拟盘成交，按时间倒序；初始化建仓单独标注。银行真实成交与条件单在账户页查看。",
  plans:
    "AI 模拟盘决策，按时间倒序。包含买卖委托、持有、观望和撤单；决策条数不等于成交次数。",
  cycles: "从开仓到全部卖完算一个完整周期；部分卖出不算完成一个周期。",
  analysis: "模型每轮分析的结果，一轮包含全部活动账户决策，不代表已经成交。",
  alerts:
    "系统事件与内部复评触发记录，不代表 AI 已下单；旧规则提醒仅保留历史记录，不再生成或推送。",
};
let generation = 0,
  controller;
async function load(reset = true) {
  if (!reset && busy.value) return;
  const run = ++generation;
  controller?.abort();
  controller = new AbortController();
  busy.value = true;
  error.value = "";
  const timer = setTimeout(() => controller?.abort(), 15000);
  try {
    const query = new URLSearchParams({
      ...filter,
      compact: "1",
      limit: "20",
      offset: String(reset ? 0 : rows.value.length),
    });
    const { value } = await requestJSON("/api/history?" + query, {
      signal: controller.signal,
    });
    if (run === generation) {
      rows.value = reset ? value.items : [...rows.value, ...value.items];
      more.value = value.has_more;
    }
  } catch (e) {
    if (run === generation)
      error.value =
        e.name === "AbortError" ? "历史加载超时，请重试" : e.message;
  } finally {
    clearTimeout(timer);
    if (run === generation) busy.value = false;
  }
}
function changeKind(kind) {
  filter.kind = kind;
  filter.account = "all";
}
watch(
  () => JSON.stringify(filter),
  () => {
    rows.value = [];
    load(true);
  },
);
watch(
  () => props.data,
  () => {
    if (
      filter.model !== "all" &&
      !props.data.model_controls.some(
        (c) => c.key === filter.model && c.enabled,
      )
    )
      filter.model = "all";
    if (filter.account !== "all" && !props.data.strategy_names[filter.account])
      filter.account = "all";
    if (currentPage.value === "records") load(true);
  },
  { immediate: true },
);
onUnmounted(() => controller?.abort());
function live() {
  accountView.value = "live";
  navigate("strategies");
}
</script>
<template>
  <section id="panel-records" class="page-stack" data-page-ready="true">
    <Card title="模拟盘操盘记录" class="record-controls"
      ><template #action
        ><button class="link-btn" @click="live">
          真实账户与建议 →
        </button></template
      >
      <div class="chips">
        <button
          v-for="[key, label] in kinds"
          :key="key"
          class="filter-btn"
          type="button"
          :aria-pressed="filter.kind === key"
          @click="changeKind(key)"
        >
          {{ label }}
        </button>
      </div>
      <div class="filters" style="margin-top: 12px">
        <label
          >筛选模型<select
            v-model="filter.model"
            aria-label="筛选模型"
            @change="filter.account = 'all'"
          >
            <option v-for="[k, t] in modelChoices(data)" :key="k" :value="k">
              {{ t }}
            </option>
          </select></label
        ><label
          >筛选时间<select v-model="filter.days" aria-label="筛选时间">
            <option value="all">全部历史</option>
            <option value="1">最近24小时</option>
            <option value="7">最近7天</option>
            <option value="30">最近30天</option>
          </select></label
        ><label v-if="!['analysis', 'alerts'].includes(filter.kind)"
          >筛选账户<select v-model="filter.account" aria-label="筛选账户">
            <option value="all">全部账户</option>
            <option
              v-for="[k, t] in Object.entries(data.strategy_names || {}).filter(
                ([key]) =>
                  filter.model === 'all' || key.startsWith(filter.model + ':'),
              )"
              :key="k"
              :value="k"
            >
              {{ t }}
            </option>
          </select></label
        >
      </div></Card
    >
    <div class="notice info">{{ descriptions[filter.kind] }}</div>
    <Card
      :title="kinds.find((k) => k[0] === filter.kind)?.[1]"
      :subtitle="`已显示 ${visibleRows.length} 条${more ? ' · 还有更多' : ''}`"
      ><ul v-if="visibleRows.length" class="list">
        <template
          v-for="r in visibleRows"
          :key="`${filter.kind}:${r.account}:${r.id || r.stamp}`"
          ><TradeRecord
            v-if="['fills', 'plans'].includes(filter.kind)"
            :record="r"
            :data="data"
            :kind="filter.kind"
          />
          <li v-else-if="filter.kind === 'cycles'">
            <div class="row">
              <strong class="grow"
                >{{ accountName(r.account, data) }} · #{{ r.id }} ·
                {{ r.closed_at ? "已平仓" : "持仓中" }}</strong
              ><span class="sub">{{ fmtTs(r.stamp) }}</span>
            </div>
            <div class="sub">
              开仓 {{ fmtTs(r.opened_at) }} · 平仓 {{ fmtTs(r.closed_at) }}
            </div>
            <div>
              {{
                r.closed_at
                  ? "已实现盈亏 " + num(r.pnl ?? r.realized_cny) + " 元"
                  : "已回收金额 " + num(r.proceeds) + " 元，完整周期尚未结束"
              }}
            </div>
          </li>
          <li v-else-if="filter.kind === 'analysis'">
            <div class="row">
              <strong class="grow"
                >{{
                  r.account === "codex"
                    ? "Codex"
                    : r.account === "claude"
                      ? "Claude"
                      : r.account
                }}
                · {{ r.error ? "失败" : "成功" }}</strong
              ><span class="sub">{{ fmtTs(r.stamp) }}</span>
            </div>
            <div class="sub">{{ r.trigger }}</div>
            <div>{{ r.error ? "失败：" + r.error : r.view?.summary }}</div>
            <div class="sub">
              短期{{ bias[r.view?.bias_short] || "—" }} · 中长期{{
                bias[r.view?.bias_long] || "—"
              }}
            </div>
            <div v-if="r.view?._performance" class="sub">
              耗时 {{ num(r.view._performance.wall_seconds, 1) }} 秒 · 输入
              {{ num(r.view._performance.input_tokens, 0) }} / 缓存
              {{ num(r.view._performance.cached_input_tokens, 0) }} / 输出
              {{ num(r.view._performance.output_tokens, 0) }} Token
            </div>
            <div v-if="r.outcomes" class="sub">
              {{
                Object.entries(r.outcomes)
                  .map(
                    ([k, o]) =>
                      `${k}：${o.status === "done" ? (o.hit ? "命中 " : "未命中 ") + pct(o.ret) : o.status === "pending" ? "待到期" : "缺数据"}`,
                  )
                  .join(" · ")
              }}
            </div>
          </li>
          <li v-else>
            <div class="row">
              <strong class="grow">{{ displayTitle(r.title) }}</strong
              ><span class="sub">{{ fmtTs(r.stamp) }}</span>
            </div>
            <div class="sub">{{ r.delivered ? "服务已受理" : "仅记录" }}</div>
          </li></template
        >
      </ul>
      <div v-else class="empty">
        {{ busy ? "正在加载历史…" : error || "暂无匹配记录" }}
      </div>
      <div v-if="error" class="notice warn" role="status">{{ error }}</div>
      <button
        v-if="more || error"
        class="link-btn"
        :disabled="busy"
        @click="load(!more)"
      >
        {{ busy ? "加载中…" : error ? "重试" : "加载更多" }}
      </button></Card
    >
  </section>
</template>
