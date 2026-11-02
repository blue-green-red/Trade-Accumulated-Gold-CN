<script setup>
import { computed, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
import { num, signed, fmtTs } from "../../format";
const props = defineProps({ data: Object });

import Chart from "../../components/Chart.vue";
import SourceMeta from "../../components/SourceMeta.vue";
import { navigate, diagnosticsOpen } from "../../composables/dashboard";
const macro = computed(() => props.data.macro || {});
const reference = computed(
  () => props.data.wave_evidence?.daily_reference || {},
);
const indicator = computed(
  () => props.data.wave_evidence?.daily_indicators || {},
);
const fund = computed(() => props.data.jd_research?.funds || {});
const orders = computed(() => props.data.jd_research?.orders || {});
const points = computed(() =>
  (reference.value.recent_sessions || [])
    .filter((r) => Number.isFinite(r.close_usd))
    .map((r) => [r.date, r.close_usd]),
);
function diagnostics() {
  diagnosticsOpen.value = true;
  navigate("ai");
}
const statusLabels = {
  running: "结果待记录",
  fresh: "参考数据可用",
  stale: "来源数据过期",
  cache_stale: "缓存待刷新",
  unavailable: "获取失败",
  not_fetched: "等待分析时获取",
  unconfigured: "尚未配置",
  quota_exhausted: "京东额度耗尽",
  budget_exhausted: "本地预算用完",
  rate_limited: "请求频率受限",
  refreshing: "正在获取",
  no_data: "暂无源数据",
};

const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div
    id="market-reference"
    class="page-stack"
    data-market-view="reference"
    role="tabpanel"
    aria-label="市场参考"
  >
    <Card title="宏观指标"
      ><SourceMeta
        :stamp="macro.quotes_fetched_at || macro.fetched_at"
        cadence="每5分钟"
        :max="420"
        :error="macro.refresh_errors?.quotes"
        :missing="!Object.keys(macro.quotes || {}).length"
      />
      <div class="macro-quotes">
        <div
          v-for="[name, unit, digits] in [
            ['美元指数', '点', 2],
            ['美国10年期国债收益率', '%', 3],
            ['美国2年期国债收益率', '%', 3],
            ['离岸人民币 USD/CNH', 'USD/CNH', 4],
          ]"
          :key="name"
          class="quote-tile"
        >
          <div class="sub">{{ name }}</div>
          <div class="quote-value">
            {{ num(macro.quotes?.[name]?.value, digits)
            }}<span class="quote-unit">{{ unit }}</span>
          </div>
          <div class="sub">
            {{
              macro.quotes?.[name]?.change_pct == null
                ? "暂无涨跌数据"
                : "较前收 " + signed(macro.quotes[name].change_pct) + "%"
            }}
          </div>
        </div>
      </div></Card
    >
    <details class="more card">
      <summary>日线技术指标</summary>
      <div class="asset-filters">
        <div>
          <div class="sub">RSI（日线）</div>
          <strong>{{ num(indicator.rsi, 1) }}</strong>
        </div>
        <div>
          <div class="sub">MA20（美元/盎司）</div>
          <strong>{{ num(indicator.ma20_usd) }}</strong>
        </div>
        <div>
          <div class="sub">MA50（美元/盎司）</div>
          <strong>{{ num(indicator.ma50_usd) }}</strong>
        </div>
      </div>
      <p class="sub">
        TradingView现货日线 ·
        {{
          {
            fresh: "已更新",
            partial: "部分指标缺失",
            stale: "指标已过期",
            unavailable: "尚无有效数据",
          }[indicator.status] || "尚无有效数据"
        }}
        · 指标时间 {{ fmtTs(indicator.fetched_at) }} · 日线指标盘中会变化
      </p>
    </details>
    <details class="more card">
      <summary>国际黄金期货历史参考</summary>
      <Chart
        unit="美元/盎司"
        :series="[{ key: 'futures', name: '黄金期货历史收盘', points }]"
        empty-text="已结束日期的日线缓存尚未积累"
      />
      <p class="sub">
        Yahoo Finance GC=F · 美元/盎司 · 数据截至
        {{ reference.last_day || "—" }} ·
        仅开收盘，用于观察趋势，京东成交以银行报价为准。
      </p>
      <div class="chips">
        <span
          v-for="[key, value] in Object.entries(reference.changes_pct || {})"
          :key="key"
          class="chip"
          >{{ key.replace("d", "日") }} {{ signed(value) }}%</span
        >
      </div>
    </details>
    <details class="more card">
      <summary>京东资金与挂单参考</summary>
      <p class="sub">
        资金数据为最近一次事件；挂单价格沿用来源口径，单位未明确，不作为积存金成交价格。
      </p>
      <div class="grid-2">
        <Card title="大额成交 · 多空占比"
          ><span
            class="data-state"
            :class="{ warn: fund.status !== 'fresh' || fund.refresh_status }"
            >{{ statusLabels[fund.status] || "暂无数据"
            }}{{
              fund.status === "fresh" && fund.refresh_status
                ? " · " + (statusLabels[fund.refresh_status] || "刷新暂停")
                : ""
            }}</span
          ><template v-if="fund.status === 'fresh'"
            ><div
              class="flow-bar"
              role="img"
              :aria-label="`多单 ${num(fund.long_pct, 1)}%，空单 ${num(fund.short_pct, 1)}%`"
            >
              <span
                :style="{
                  width:
                    (fund.long_pct / (fund.long_pct + fund.short_pct || 1)) *
                      100 +
                    '%',
                  background: 'var(--series-1)',
                }"
              ></span
              ><span style="flex: 1; background: var(--series-2)"></span>
            </div>
            <div class="row" style="justify-content: space-between">
              <span>多单 {{ num(fund.long_pct, 1) }}%</span
              ><span>空单 {{ num(fund.short_pct, 1) }}%</span>
            </div>
            <p class="sub">
              事件成交额 {{ num(fund.volume_usd / 10000, 1) }} 万美元
            </p></template
          >
          <div v-else class="empty">暂无可用资金事件</div>
          <div class="data-meta">
            源发布 {{ fmtTs(fund.published_at) }} · 成功获取
            {{ fmtTs(fund.fetched_at) }}
          </div> </Card
        ><Card title="挂单密集区"
          ><span
            class="data-state"
            :class="{
              warn: orders.status !== 'fresh' || orders.refresh_status,
            }"
            >{{ statusLabels[orders.status] || "暂无数据"
            }}{{
              orders.status === "fresh" && orders.refresh_status
                ? " · " + (statusLabels[orders.refresh_status] || "刷新暂停")
                : ""
            }}</span
          ><template v-if="orders.status === 'fresh'"
            ><div class="cluster-row sub">
              <div>
                多单
                <div>价格 / 占比</div>
              </div>
              <div>
                空单
                <div>价格 / 占比</div>
              </div>
            </div>
            <div v-for="i in 3" :key="i" class="cluster-row">
              <span>{{
                orders.long_clusters?.[i - 1]
                  ? num(orders.long_clusters[i - 1].price) +
                    " / " +
                    num(orders.long_clusters[i - 1].pct) +
                    "%"
                  : "—"
              }}</span
              ><span>{{
                orders.short_clusters?.[i - 1]
                  ? num(orders.short_clusters[i - 1].price) +
                    " / " +
                    num(orders.short_clusters[i - 1].pct) +
                    "%"
                  : "—"
              }}</span>
            </div></template
          >
          <div v-else class="empty">暂无可用挂单分布</div>
          <div class="data-meta">
            源发布 {{ fmtTs(orders.published_at) }} · 成功获取
            {{ fmtTs(orders.fetched_at) }}
          </div>
        </Card>
      </div>
      <p class="sub">
        <a
          href="https://youqian.jd.com/"
          target="_blank"
          rel="noopener noreferrer"
          >数据由京东金融有钳提供</a
        >
      </p>
    </details>
    <button class="link-btn" @click="diagnostics">接口状态与额度 →</button>
  </div>
</template>
