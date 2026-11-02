<script setup>
import { computed, ref, watch } from "vue";
import Card from "../components/Card.vue";
import MarketSummary from "../components/MarketSummary.vue";
import SourceMeta from "../components/SourceMeta.vue";
import Chart from "../components/Chart.vue";
import { num, signed, fmtTs } from "../format";
import { eventName } from "../events";
const props = defineProps({ data: Object });
const wave = ref("jd"),
  calendar = ref("upcoming"),
  flash = ref("important"),
  limit = ref(8),
  showPrice = ref(false),
  priceRange = ref("1d");
watch(flash, () => (limit.value = 8));
const evidence = computed(() => props.data.wave_evidence || {});
const reference = computed(() => evidence.value.daily_reference || {}),
  jdDaily = computed(() => evidence.value.jd_daily || {}),
  indicator = computed(() => evidence.value.daily_indicators || {});
const macro = computed(() => props.data.macro || {}),
  research = computed(() => props.data.jd_research || {});
const wavePoints = computed(() =>
  (wave.value === "proxy"
    ? reference.value.recent_sessions || []
    : jdDaily.value.days || []
  )
    .map((r) => [
      wave.value === "proxy" ? r.date : r.date_bj,
      wave.value === "proxy" ? r.close_usd : r.last_price_cny,
    ])
    .filter((p) => Number.isFinite(p[1])),
);
const events = computed(() => {
  const now = Date.now();
  return (macro.value.calendar || [])
    .filter(
      (e) =>
        +new Date(e.time_utc) >= now - 86400000 &&
        +new Date(e.time_utc) <= now + 7 * 86400000,
    )
    .map((e) =>
      +new Date(e.time_utc) > now ? { ...e, actual: null, released: false } : e,
    )
    .filter(
      (e) =>
        calendar.value === "all" ||
        (calendar.value === "released"
          ? e.released
          : !e.released && +new Date(e.time_utc) >= now - 1800000),
    )
    .sort((a, b) =>
      calendar.value === "released"
        ? +new Date(b.time_utc) - +new Date(a.time_utc)
        : +new Date(a.time_utc) - +new Date(b.time_utc),
    );
});
const flashes = computed(() =>
  (props.data.jin10 || []).filter((x) => flash.value === "all" || x.important),
);
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
const errorLabels = {
  LocalDailyBudget: "本地每日请求预算已用完",
  RequestInterrupted: "上次请求未记录结果，等待重试",
  ProviderDailyQuota: "供应商报告当日额度耗尽",
  ProviderRateLimit: "供应商限制请求频率",
  ProviderUnauthorized: "接口授权失败",
  ProviderRejected: "接口返回失败",
  TimeoutError: "接口请求超时",
  URLError: "网络请求失败",
  ValueError: "返回数据未通过校验",
  JSONDecodeError: "响应格式错误",
  InvalidCachedData: "缓存数据格式异常",
};
const quotaDescriptions = {
  quota_exhausted:
    "京东返回当日额度耗尽，两个参考接口暂停24小时；此时间是本地重试时间，不代表供应商确认的重置时间。",
  budget_exhausted:
    "本地每日请求预算已用完；次日北京时间额度恢复，等待分析触发更新。",
  rate_limited: "京东限制请求频率，短暂暂停两个参考接口，避免连续重试。",
};
const fund = computed(() => research.value.funds || {}),
  orders = computed(() => research.value.orders || {}),
  quota = computed(() => research.value.quota || {});
function eventStatus(e) {
  const minutes = (+new Date(e.time_utc) - Date.now()) / 60000;
  return e.released
    ? "已公布"
    : minutes < 0
      ? "已到时间 · 待更新"
      : minutes < 60
        ? Math.ceil(minutes) + " 分钟后"
        : "待公布";
}
</script>
<template>
  <section id="panel-market" class="page-stack" data-page-ready="true">
    <MarketSummary :data="data" /><Card title="波段行情与日线参考"
      ><div class="asset-filters">
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
      <div class="seg" role="group" aria-label="波段数据源">
        <button
          v-for="[key, label] in [
            ['jd', '京东逐日采样'],
            ['proxy', '国际黄金期货历史参考'],
          ]"
          :key="key"
          type="button"
          :aria-pressed="wave === key"
          @click="wave = key"
        >
          {{ label }}
        </button>
      </div>
      <Chart
        :series="[
          {
            key: wave,
            name:
              wave === 'proxy'
                ? '黄金期货历史收盘（美元/盎司）'
                : '京东末笔采样（元/克）',
            points: wavePoints,
          },
        ]"
        :empty-text="
          wave === 'proxy'
            ? '已结束日期的日线缓存尚未积累'
            : '京东逐日采样尚未积累'
        "
      />
      <p class="sub">
        {{
          wave === "proxy"
            ? `国际黄金期货历史参考 · Yahoo Finance GC=F · 美元/盎司 · 数据截至 ${reference.last_day || "—"} · ${(reference.recent_sessions || []).length} 个交易日，仅开收盘。用于观察趋势，京东成交以银行报价为准。`
            : `京东报价本地采样 · 元/克 · 北京时间自然日 · ${(jdDaily.days || []).length} 天；首末笔不是官方开收盘，采样高低不代表逐笔极值。`
        }}
      </p>
      <div v-if="wave === 'proxy'" class="chips">
        <span
          v-for="[key, value] in Object.entries(reference.changes_pct || {})"
          :key="key"
          class="chip"
          >{{ key.replace("d", "日") }} {{ signed(value) }}%</span
        >
      </div>
      <details v-else class="more">
        <summary>逐日采样明细与缺口</summary>
        <div v-if="jdDaily.missing_dates?.length" class="notice warn">
          未采到报价的交易日期：{{ jdDaily.missing_dates.join("、") }}
        </div>
        <p class="sub">
          缺口按每分钟一档估算，仅表示采样覆盖，不等于接口故障。
        </p>
        <ul class="list">
          <li
            v-for="r in (jdDaily.days || []).slice().reverse()"
            :key="r.date_bj"
          >
            <strong
              >{{ r.date_bj }} ·
              {{ r.session_ended ? "日期已结束" : "当日未结束" }} ·
              {{ r.partial ? "采样不完整" : "分钟档覆盖完整" }}</strong
            >
            <div class="sub">
              首笔 {{ num(r.first_price_cny) }} · 末笔
              {{ num(r.last_price_cny) }} · 采样低/高
              {{ num(r.sampled_low_cny) }} / {{ num(r.sampled_high_cny) }} 元/克
            </div>
            <div class="sub">
              {{ r.samples }} 次采样 · 缺
              {{ r.missing_minute_samples }} 个分钟档 · 最大采样间隔
              {{ num(r.max_gap_minutes, 1) }} 分钟
            </div>
            <div class="sub">
              {{ fmtTs(r.first_sample_at) }} 至 {{ fmtTs(r.last_sample_at) }}
            </div>
          </li>
        </ul>
      </details></Card
    ><Card title="未来七天经济日历"
      ><SourceMeta
        :stamp="macro.calendar_fetched_at || macro.fetched_at"
        :cadence="
          macro.calendar_refresh_seconds === 60
            ? '公布窗口 · 每分钟'
            : '每30分钟'
        "
        :max="macro.calendar_refresh_seconds === 60 ? 180 : 2100"
        :error="macro.refresh_errors?.calendar"
        :missing="!macro.calendar"
      />
      <div
        class="row"
        style="
          justify-content: space-between;
          flex-wrap: wrap;
          margin-top: 10px;
        "
      >
        <div class="seg" role="group" aria-label="日历筛选">
          <button
            v-for="[key, label] in [
              ['upcoming', '待公布'],
              ['released', '已公布'],
              ['all', '全部'],
            ]"
            :key="key"
            :aria-pressed="calendar === key"
            @click="calendar = key"
          >
            {{ label }}
          </button>
        </div>
        <span class="sub"
          >北京时间 · 未来7天及近24小时 · {{ events.length }} 项</span
        >
      </div>
      <ul v-if="events.length" class="event-list">
        <li v-for="e in events" :key="e.title + e.time_utc" class="event-row">
          <div class="event-time">
            {{ fmtTs(e.time_utc) }}
            <div class="data-meta">{{ eventStatus(e) }}</div>
          </div>
          <div>
            <div class="event-title">
              <span v-if="e.tier === 1" class="tag ai">重大</span>
              {{ { US: "美国", CN: "中国" }[e.country] || e.country }}
              {{ eventName(e.title) }}
            </div>
            <div class="event-values">
              <span
                >实际 <strong>{{ e.actual ?? "—" }}</strong></span
              ><span>预期 {{ e.forecast ?? "—" }}</span
              ><span>前值 {{ e.previous ?? "—" }}</span>
            </div>
          </div>
        </li>
      </ul>
      <div v-else class="empty">
        {{
          macro.calendar ? "当前筛选下暂无日程" : "日历尚未获取，等待后台更新"
        }}
      </div></Card
    ><Card title="宏观指标"
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
    ><Card title="黄金相关快讯"
      ><div class="seg" role="group" aria-label="快讯筛选">
        <button :aria-pressed="flash === 'all'" @click="flash = 'all'">
          全部快讯</button
        ><button
          :aria-pressed="flash === 'important'"
          @click="flash = 'important'"
        >
          只看重要
        </button>
      </div>
      <ul v-if="flashes.length" class="list">
        <li v-for="x in flashes.slice(0, limit)" :key="x.id">
          <div class="row">
            <span class="sub">{{ x.time_bj }}</span
            ><span v-if="x.important" class="tag ai">重要</span>
          </div>
          <div>{{ x.text }}</div>
          <div v-if="data.jin10_triage?.[x.id]" class="sub">
            {{
              { trigger: "已排队复评", skip: "未触发" }[
                data.jin10_triage[x.id].decision
              ] || data.jin10_triage[x.id].decision
            }}
            · {{ data.jin10_triage[x.id].reason }}
          </div>
        </li>
      </ul>
      <div v-else class="empty">暂无相关快讯</div>
      <button
        v-if="flashes.length > limit"
        class="link-btn"
        @click="limit += 8"
      >
        显示更多快讯 →
      </button></Card
    >
    <details class="more card" @toggle="showPrice = $event.target.open">
      <summary>分时走势（按需查看）</summary>
      <div class="seg" role="group" aria-label="时间范围">
        <button
          v-for="[key, label] in [
            ['1d', '1 天'],
            ['7d', '7 天'],
          ]"
          :key="key"
          :aria-pressed="priceRange === key"
          @click="priceRange = key"
        >
          {{ label }}
        </button>
      </div>
      <div class="sub">
        国际金价当日采样范围 {{ num(data.snapshot?.day_low) }}–{{
          num(data.snapshot?.day_high)
        }}
        美元/盎司
      </div>
      <Chart
        v-if="showPrice"
        :series="[
          {
            key: 'xau',
            name: 'XAUUSD',
            points: data.prices?.[priceRange] || [],
          },
        ]"
        empty-text="价格数据积累中"
      />
    </details>
    <details class="more card">
      <summary>京东资金挂单参考与接口诊断</summary>
      <Card title="京东黄金参考" subtitle="分析时获取 · 缓存15分钟"
        ><p class="sub">
          资金数据为最近一次事件；挂单价格沿用来源口径，单位未明确，不作为积存金成交价格。
        </p>
        <div v-if="quota.daily_cap != null" class="sub">
          后台参考请求：今日 {{ quota.used_today }}/{{ quota.daily_cap }} 次 ·
          两个模型、两个接口共享预算
        </div>
        <p class="sub">本地计数不包含历史调试或外部调用，不是京东剩余额度。</p>
        <div v-if="quotaDescriptions[quota.status]" class="notice warn">
          {{ quotaDescriptions[quota.status] }}
          <div v-if="quota.retry_at" class="sub">
            可重试时间 {{ fmtTs(quota.retry_at) }}
          </div>
        </div>
        <details v-if="quota.recent_requests?.length" class="more">
          <summary>最近接口请求</summary>
          <ul class="list">
            <li v-for="(r, i) in quota.recent_requests" :key="i">
              {{ fmtTs(r.started_at) }} ·
              {{ r.kind === "funds" ? "资金" : "挂单" }} ·
              {{ statusLabels[r.status] || r.status }}
              <div v-if="r.error_type" class="sub">
                {{ errorLabels[r.error_type] || "接口请求失败" }}
              </div>
            </li>
          </ul>
        </details>
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
            </div>
            <div v-if="fund.attempted_at" class="sub">
              最近尝试 {{ fmtTs(fund.attempted_at) }}
            </div>
            <div v-if="fund.error_type" class="sub">
              {{ errorLabels[fund.error_type] || "接口请求失败" }}
            </div>
            <div v-if="fund.retry_at" class="sub">
              暂停请求至 {{ fmtTs(fund.retry_at) }}，之后等待正常分析触发重试
            </div></Card
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
            <div v-if="orders.attempted_at" class="sub">
              最近尝试 {{ fmtTs(orders.attempted_at) }}
            </div>
            <div v-if="orders.error_type" class="sub">
              {{ errorLabels[orders.error_type] || "接口请求失败" }}
            </div>
            <div v-if="orders.retry_at" class="sub">
              暂停请求至 {{ fmtTs(orders.retry_at) }}，之后等待正常分析触发重试
            </div></Card
          >
        </div>
        <div class="data-meta">
          <a
            href="https://youqian.jd.com/"
            target="_blank"
            rel="noopener noreferrer"
            >数据由京东金融有钳提供</a
          >
        </div></Card
      >
    </details>
  </section>
</template>
