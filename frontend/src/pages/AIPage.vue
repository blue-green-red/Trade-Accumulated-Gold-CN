<script setup>
import { computed, defineAsyncComponent, ref, watch } from "vue";
import Card from "../components/Card.vue";
import {
  pending,
  actionErrors,
  toggleModel,
  manualAnalysis,
  navigate,
  accountView,
} from "../composables/dashboard";
import {
  num,
  pct,
  rate,
  fmtTs,
  modelNames,
  bias,
  accountLabels,
  actions,
  periodChoices,
  modelChoices,
  riskMode,
  statusLabel,
  validity,
  executionRange,
  safeUrl,
} from "../format";
const props = defineProps({ data: Object });
const model = ref(
    props.data.model_controls.find((c) => c.enabled)?.key || "all",
  ),
  period = ref("all"),
  start = ref("all"),
  showScore = ref(false);
const ForecastCard = defineAsyncComponent(
  () => import("../components/ForecastCard.vue"),
);
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
const accounts = computed(() =>
  (props.data.autonomous?.accounts || []).filter(
    (a) =>
      (model.value === "all" || a.model === model.value) &&
      (period.value === "all" || a.start_mode.startsWith(period.value + "_")) &&
      (start.value === "all" || a.start_mode.endsWith("_" + start.value)),
  ),
);
const analysts = computed(() => props.data.ai?.analysts || []);
const manualPending = (c) => ["queued", "running"].includes(c.manual?.status);
const manualDisabled = (c) =>
  pending["manual:" + c.key] ||
  c.available !== true ||
  manualPending(c) ||
  (c.runs_today || 0) >= (c.daily_cap || 120) - (c.urgent_reserve || 0);
function live() {
  accountView.value = "live";
  navigate("strategies");
}
</script>
<template>
  <section id="panel-ai" class="page-stack" data-page-ready="true">
    <Card title="AI 操盘控制" subtitle="模型独立分析，关闭后隐藏相关数据"
      ><div class="grid-2">
        <div v-for="c in data.model_controls" :key="c.key">
          <div class="row">
            <strong class="grow">{{ c.name }}</strong
            ><span class="tag">{{ c.enabled ? "已开启" : "已关闭" }}</span
            ><button
              class="chip-btn"
              type="button"
              role="switch"
              :aria-label="c.name + ' 操盘'"
              :aria-checked="!!c.enabled"
              :disabled="pending['model:' + c.key]"
              @click="toggleModel(c.key, !c.enabled)"
            >
              {{ c.enabled ? "关闭模型" : "开启模型" }}
            </button>
          </div>
          <div v-if="c.enabled" class="row" style="margin-top: 10px">
            <button
              class="chip-btn"
              :aria-label="c.name + ' 立即分析并操盘'"
              :disabled="manualDisabled(c)"
              @click="manualAnalysis(c.key)"
            >
              {{
                manualPending(c)
                  ? c.manual.status === "running"
                    ? "手动分析中"
                    : "手动请求已排队"
                  : c.runtime?.running || c.runtime?.queued
                    ? "排队分析并操盘"
                    : "立即分析并操盘"
              }}
            </button>
          </div>
          <div class="sub" role="status" style="margin-top: 8px">
            {{
              !c.enabled
                ? "模型已关闭，相关数据已隐藏；重新开启后恢复展示"
                : c.available === false
                  ? "分析后端暂不可用"
                  : c.runtime?.running
                    ? "正在分析 · 串行运行"
                    : c.runtime?.queued
                      ? "排队中 · 等待前一个模型完成"
                      : "已开启，按调度分析；重新开启后先获取新判断"
            }}
          </div>
          <div v-if="c.enabled" class="sub">
            今日调用 {{ c.runs_today || 0 }} / {{ c.daily_cap }} 轮 · 其中
            {{ c.urgent_reserve }} 轮预留重大事件<span v-if="c.pending_events">
              · 待覆盖 {{ c.pending_events }} 个新事件</span
            >
          </div>
          <div v-if="c.manual?.message" class="sub">
            {{ manualPending(c) ? "手动请求" : "上次手动" }}：{{
              c.manual.message
            }}
          </div>
          <div
            v-if="
              actionErrors['model:' + c.key] || actionErrors['manual:' + c.key]
            "
            class="notice warn"
            role="alert"
          >
            {{
              actionErrors["model:" + c.key] || actionErrors["manual:" + c.key]
            }}
          </div>
        </div>
      </div>
      <p class="sub">
        手动触发一次活动账户分析并按条件操盘，不保证成交；休市或无有效报价时等待，不改变自动分析频率。
      </p></Card
    >
    <div v-if="analysts.length >= 2" class="notice info">
      两个 AI：短期{{
        analysts[0].bias_short === analysts[1].bias_short ? "一致" : "分歧"
      }}，中长期{{
        analysts[0].bias_long === analysts[1].bias_long ? "一致" : "分歧"
      }}
      · 两模型独立判断，概率用于预测评估
    </div>
    <div v-if="analysts.length" class="grid-2">
      <Card
        v-for="v in analysts"
        :key="v.analyst"
        :title="v.name"
        :subtitle="`${fmtTs(v.ts)} · ${v.model || ''}`"
        ><div class="mini-stats" style="margin-top: 0">
          <div class="mini-stat">
            <div class="l">短期 1-3 日</div>
            <div class="v">{{ bias[v.bias_short] }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">中长期 1-3 月</div>
            <div class="v">{{ bias[v.bias_long] }}</div>
          </div>
          <div class="mini-stat">
            <div class="l">置信</div>
            <div class="v">
              {{
                { low: "低", medium: "中", high: "高" }[v.confidence] ||
                v.confidence
              }}
            </div>
          </div>
        </div>
        <div v-if="v.p_up_3d != null" class="sub" style="margin-top: 8px">
          3 日 涨&gt;0.4% {{ rate(v.p_up_3d) }} · 跌&gt;0.4%
          {{ rate(v.p_down_3d) }} ｜ 3 个月 涨&gt;4% {{ rate(v.p_up_3m) }} ·
          跌&gt;4% {{ rate(v.p_down_3m) }}
        </div>
        <p>{{ v.summary }}</p>
        <div
          v-if="
            data.model_controls.find((c) => c.key === v.analyst)?.changed_at &&
            new Date(v.ts) <=
              new Date(
                data.model_controls.find((c) => c.key === v.analyst).changed_at,
              )
          "
          class="notice warn"
        >
          已重新开启，等待新判断；以上判断不用于开仓。
        </div>
        <div v-if="v._price_guard?.invalidated_at" class="notice warn">
          {{ v._price_guard.reason }}；等待最新快照复查。已有持仓保护继续有效。
        </div>
        <div v-if="v.hard_risk_event" class="notice warn">
          {{ v.hard_risk_note }}
        </div>
        <div v-if="v._completed_at" class="sub">
          行情快照 {{ fmtTs(v._snapshot_at) }} · 开始
          {{ fmtTs(v._started_at) }} · 完成 {{ fmtTs(v._completed_at) }}
        </div>
        <details v-if="v._performance" class="more">
          <summary>本轮分析效率</summary>
          <div class="sub">
            总耗时 {{ num(v._performance.wall_seconds, 1) }} 秒 · 排队
            {{ num(v._performance.queue_seconds || 0, 1) }} 秒 ·
            {{
              v._performance.mode === "resume"
                ? "续接第" + v._performance.session_turn + "轮"
                : "独立新会话"
            }}
          </div>
          <div class="sub">
            输入 {{ num(v._performance.input_tokens, 0) }} Token · 缓存
            {{ num(v._performance.cached_input_tokens, 0) }} · 输出
            {{ num(v._performance.output_tokens, 0) }} Token
          </div>
          <div class="sub">
            上下文 {{ num(v._performance.input_chars_before, 0) }} →
            {{ num(v._performance.input_chars_after, 0) }} 字符 · 补搜
            {{ v._performance.searches || 0 }} 次
          </div>
          <div class="sub">
            账户历史查询 {{ v._performance.history_queries || 0 }} 次 · 返回
            {{ v._performance.history_rows || 0 }} 条记录
          </div>
          <div v-if="v._performance.first_event_seconds != null" class="sub">
            首个模型事件
            {{ num(v._performance.first_event_seconds, 1) }} 秒（不是首Token）
          </div>
        </details>
        <details v-if="v.drivers?.length" class="more">
          <summary>驱动因素 {{ v.drivers.length }}</summary>
          <ul class="list">
            <li v-for="(x, i) in v.drivers" :key="i">
              <strong>{{ x.direction }} · {{ x.horizon }}</strong>
              {{ x.factor }}
              <div class="sub">{{ x.evidence }}</div>
            </li>
          </ul>
        </details>
        <details v-if="v.upcoming_events?.length" class="more">
          <summary>日程 {{ v.upcoming_events.length }}</summary>
          <ul class="list">
            <li v-for="(e, i) in v.upcoming_events" :key="i">
              {{ e.time_bj }} {{ e.name }}
              <div class="sub">{{ e.impact }}</div>
            </li>
          </ul>
        </details>
        <details v-if="v.sources?.length" class="more">
          <summary>来源 {{ v.sources.length }}</summary>
          <ul class="list">
            <li v-for="(s, i) in v.sources" :key="i">
              <a
                :href="safeUrl(s.url)"
                target="_blank"
                rel="noopener noreferrer nofollow"
                >{{ s.title || s.url }}</a
              >
            </li>
          </ul>
        </details>
        <div v-if="v.account_decisions" class="sub">
          本轮已统一提交{{
            Object.keys(v.account_decisions).length
          }}份账户决策，当前活动账户执行状态见下方卡片。
        </div>
        <button
          v-if="Object.values(v.live_account_advice || {}).some(Boolean)"
          class="link-btn"
          @click="live"
        >
          查看真实账户建议 →
        </button></Card
      >
    </div>
    <Card v-else title="最新分析"
      ><div class="empty">
        {{
          data.ai?.enabled
            ? "等待首次分析，满足价格变化、事件或兜底条件时排队"
            : "分析后端尚未就绪，请查看上方模型状态"
        }}
      </div></Card
    ><Card
      :title="
        riskMode(data)
          ? '六账户短线风险实验'
          : data.trading_mode === 'four_account'
            ? '四账户自主操盘'
            : '六账户自主操盘'
      "
      subtitle="每户10万元 · 一轮统一决策"
      ><div class="filters">
        <label
          >账户模型<select v-model="model" aria-label="账户模型">
            <option v-for="[k, t] in modelChoices(data)" :key="k" :value="k">
              {{ t }}
            </option>
          </select></label
        ><label
          >{{ riskMode(data) ? "风险场景" : "账户周期"
          }}<select
            v-model="period"
            :aria-label="riskMode(data) ? '风险场景' : '账户周期'"
          >
            <option v-for="[k, t] in periodChoices(data)" :key="k" :value="k">
              {{ t }}
            </option>
          </select></label
        ><label
          >初始仓位<select v-model="start" aria-label="初始仓位">
            <option value="all">全部起点</option>
            <option value="full">初始满仓</option>
            <option value="empty">初始空仓</option>
          </select></label
        >
      </div>
      <p class="sub">
        周期复查：{{
          data.cadence?.review_minutes > 0
            ? data.cadence.review_minutes + " 分钟后，"
            : "无最小时间间隔，"
        }}国际金价较上次分析变化≥{{ data.cadence?.min_move_pct }}%才启动；最长{{
          (data.cadence?.max_idle_minutes || 240) / 60
        }}小时兜底。重大事件另外排队，执行器每分钟检查。
      </p>
      <p class="sub">
        模拟盘按数日至一两周波段运行；真实账户建议在账户页查看。
      </p>
      <button
        class="link-btn"
        style="margin-bottom: 12px"
        @click="
          navigate('records', {
            kind: 'fills',
            model,
            account: 'all',
            days: 'all',
          })
        "
      >
        查看操盘记录 →
      </button>
      <div class="grid-2">
        <Card
          v-for="a in accounts"
          :key="a.key"
          :title="`${modelNames[a.model]} · ${accountLabels[a.start_mode]}`"
          :subtitle="a.started ? '已开始' : '等待开账'"
          ><div class="sub">
            当前保护：止损
            {{
              a.stop_price == null ? "未设置" : num(a.stop_price) + " 元/克"
            }}
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
        >
      </div>
      <div v-if="!accounts.length" class="empty">当前筛选下暂无账户</div>
      <details class="more" data-detail-key="trading-policy">
        <summary>风险预算与执行规则</summary>
        <p class="sub">
          三档风险：单次计划损失1%/2%/4%，阶段回撤线5%/10%/20%，触线暂停新增买入。计划风险参考不自动止损，实际损失可能超预算。短线按数日至一两周运行，每周约两次买卖操作为节奏参考，不要求凑次数。AI
          自主决定数量与保护价，可设置、放宽或撤销止损止盈；不设置时不按固定亏损自动平仓。账户间不转账；初始满仓仅在开账时买入一次。未成交委托到期撤销，已有持仓保护继续有效。
        </p>
      </details></Card
    >
    <details class="more card">
      <summary>本轮分析输入详情</summary>
      <Card
        v-if="data.ai_inputs"
        title="共同分析数据"
        :subtitle="`最近一次准备 ${fmtTs(data.ai_inputs.at)}`"
        ><p class="sub">
          两个模型共用基础数据，串行独立判断；允许按预算补搜遗漏信息。此处记录当时输入，最新数据见行情页。
        </p>
        <div class="input-stats">
          <span class="chip">快讯 {{ data.ai_inputs.news_count }} 条</span
          ><span class="chip"
            >补搜预算 {{ data.ai_inputs.max_searches }} 次 / 模型</span
          >
        </div>
        <div class="sub">
          宏观缓存 {{ fmtTs(data.ai_inputs.macro_at) }} · 上下文
          {{ data.ai_inputs.context_chars }} 字符（不等于 Token 数）
        </div>
        <div class="sub">
          京东资金 {{ data.ai_inputs.jd_research?.funds?.status || "未知" }} ·
          挂单 {{ data.ai_inputs.jd_research?.orders?.status || "未知" }}
        </div></Card
      ><Card title="本轮实际输入的波段数据"
        ><div
          v-for="v in analysts"
          :key="v.analyst"
          style="margin-bottom: 14px"
        >
          <strong>{{ v.name }} · 快照 {{ fmtTs(v.wave_input?.as_of) }}</strong>
          <div class="sub">
            期货代理开收盘
            {{ v.wave_input?.daily_reference?.sessions || 0 }} 个交易日 ·
            最新结束日期 {{ v.wave_input?.daily_reference?.last_day || "—" }}
          </div>
          <div class="sub">
            日线指标 {{ v.wave_input?.daily_indicators?.status || "不可用" }} ·
            京东逐日采样 {{ v.wave_input?.jd_daily?.days || 0 }} 天
          </div>
          <div class="sub">
            日历未来 {{ v.wave_input?.calendar?.days_ahead || 7 }} 天 · 本轮
            {{ v.wave_input?.calendar?.events || 0 }} 项{{
              v.wave_input?.calendar?.status !== "available"
                ? "（日历缓存不可用或未记录状态）"
                : v.wave_input?.calendar?.truncated
                  ? "（已优先截取重大事件）"
                  : ""
            }}
          </div>
        </div>
        <p class="sub">
          对应各模型最新分析的实际输入；行情页显示最新缓存。采样摘要不作为官方完整K线。
        </p>
        <button class="link-btn" @click="navigate('market')">
          查看最新波段数据 →
        </button></Card
      >
    </details>
    <details class="more card" @toggle="showScore = $event.target.open">
      <summary>预测准确度（区别于交易胜率）</summary>
      <ForecastCard v-if="showScore" :controls="data.model_controls" />
    </details>
    <Card title="分析历史"
      ><template #action
        ><button
          class="link-btn"
          @click="
            navigate('records', {
              kind: 'analysis',
              model: 'all',
              account: 'all',
              days: 'all',
            })
          "
        >
          查看完整分析历史 →
        </button></template
      >
      <p class="sub">
        包含分析时间、触发原因、成功/失败，以及已到期的方向判断结果。
      </p></Card
    >
  </section>
</template>
