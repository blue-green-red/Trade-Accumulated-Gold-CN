<script setup>
import { computed } from "vue";
import LiveRiskSummary from "./LiveRiskSummary.vue";
import DownsideReview from "./DownsideReview.vue";
import OrderImpact from "./OrderImpact.vue";
import {
  actions,
  modelNames,
  quantity,
  num,
  fmtTs,
  pastDeadline,
} from "../format";
const props = defineProps({
  account: Object,
  advice: Object,
  quoteFresh: Boolean,
  section: { type: String, default: "advice" },
});
const states = {
  current: "有效 · 人工参考",
  review_required: "建议需核对",
  expired: "建议已到期",
  account_changed: "账户已变化 · 需重分析",
  data_unavailable: "账户数据待更新",
};
const orderStates = {
  pending: "已挂单 · 等待成交",
  partially_filled: "部分成交",
  filled: "已成交",
  cancelled: "已撤单",
  expired: "条件单已到期",
  failed: "挂单失败",
  unknown: "状态待核对",
};
const monitorStates = {
  monitoring: "监控中",
  queued: "已触发 · 待复评",
  reviewed: "已触发 · 已纳入复评",
  latched: "持续满足 · 不重复触发",
  market_closed: "休市暂停",
  expired: "已到期",
  account_changed: "账户或委托变化 · 已失效",
  data_unavailable: "等待新鲜报价或挂单明细",
  order_inactive: "目标挂单已结束或待更新",
  review_required: "需核对",
};
const state = computed(() =>
  pastDeadline(props.advice)
    ? "expired"
    : ["current", "review_required"].includes(props.advice.status) &&
        !props.account.connected
      ? "data_unavailable"
      : props.advice.status,
);
const partState = computed(() =>
  state.value === "review_required"
    ? props.advice.action_status || state.value
    : state.value,
);
const stateLabel = computed(() =>
  state.value === "review_required" && partState.value === "current"
    ? "部分建议需核对"
    : states[state.value] || "状态待核对",
);
const symbol = {
  at_or_above: "≥",
  at_or_below: "≤",
  reference: "参考价",
  none: "未设价格条件",
};
const original = (item) =>
  item.input_order ||
  (props.account.pending_orders?.records || []).find(
    (o) => o.order_key === item.order_key,
  ) ||
  {};
const terms = (o) => {
  const prices =
    o.kind === "take_profit_stop_loss"
      ? `止盈 ≥ ${o.price_cny == null ? "待确认" : num(o.price_cny)} / 止损 ≤ ${o.loss_price_cny == null ? "待确认" : num(o.loss_price_cny)} 元/克`
      : `${symbol[o.price_condition] || "参考"} ${o.price_cny == null ? "价格待确认" : num(o.price_cny) + " 元/克"}`;
  return `${actions[o.action] || ""}${quantity(o.grams)} · ${prices}`;
};
const itemState = (item) =>
  partState.value !== "current"
    ? partState.value
    : item.state || "review_required";
const conditionState = (c) =>
  partState.value !== "current"
    ? partState.value
    : !props.quoteFresh
      ? "data_unavailable"
      : c.monitoring?.status;
const associated = (c) =>
  (props.account.pending_orders?.records || []).find(
    (o) => o.order_key === c.order_key,
  );
const thesisStates = {
  new: "首次判断",
  valid: "原假设仍成立",
  weakened: "原假设已削弱",
  invalidated: "原假设已失效",
};
const proposalNames = {
  limit_buy: "限价买入",
  limit_sell: "限价卖出",
  take_profit_stop_loss: "止盈止损卖出",
};
const proposalState = (order) => {
  const progress = order.execution_progress;
  if (progress?.orders?.length)
    return `${orderStates[progress.status] || "存在相近挂单"} · 关联推断`;
  if (pastDeadline(order)) return "条件单方案已到期";
  if (partState.value !== "current") return states[partState.value] || "待更新";
  if (order.checks?.length) return "参数需核对";
  return "待你确认挂单";
};
</script>
<template>
  <article
    class="live-advice"
    :class="{
      inactive: ['expired', 'account_changed', 'data_unavailable'].includes(
        state,
      ),
      'needs-review': state === 'review_required',
    }"
  >
    <div class="live-advice-heading">
      <strong
        >{{ modelNames[advice.analyst] || advice.analyst }} ·
        {{
          section === "orders"
            ? "挂单复评"
            : (actions[advice.action] || "待核对") + "建议"
        }}</strong
      ><span class="live-advice-status">{{ stateLabel }}</span>
    </div>
    <template v-if="section !== 'orders'">
      <LiveRiskSummary
        :risk="advice.input_account_risk"
        :profile="advice.risk_profile"
        :position="advice.input_position_pct"
        historical
      />
      <div class="live-advice-values">
        <div>
          <div class="l">即时买卖数量</div>
          <div class="v">
            {{
              advice.grams == null
                ? ["buy", "sell"].includes(advice.action)
                  ? "数量待确认"
                  : "本轮不立即买卖"
                : quantity(advice.grams)
            }}
          </div>
        </div>
        <div>
          <div class="l">建议价格条件</div>
          <div class="v">
            {{ symbol[advice.price_condition] || "价格条件待核对" }}
            {{
              advice.price_cny == null ? "" : num(advice.price_cny) + " 元/克"
            }}
          </div>
        </div>
      </div>
      <div class="live-advice-time">
        分析 {{ fmtTs(advice.created_at) }} · 到期
        {{ fmtTs(advice.expires_at) }} · 北京时间
      </div>
      <div v-if="advice.input_strategy_review" class="live-quote">
        <strong>本轮复核背景</strong>
        <div
          v-if="advice.input_strategy_review.move_since_previous_pct != null"
          class="sub"
        >
          银行报价较上轮输入
          {{
            advice.input_strategy_review.move_since_previous_pct > 0 ? "+" : ""
          }}{{ num(advice.input_strategy_review.move_since_previous_pct) }}%
        </div>
        <div class="sub">
          最近{{
            advice.input_strategy_review.lookback_rounds ||
            advice.input_strategy_review.same_plan_rounds
          }}轮中，同一交易方案连续
          {{ advice.input_strategy_review.same_plan_rounds }} 轮，其中
          {{ advice.input_strategy_review.weakened_rounds }} 轮判断削弱或失效。
        </div>
        <div
          v-if="advice.input_strategy_review.revalidate_unchanged_plan"
          class="sub"
        >
          本轮重点复核旧价位，以及继续等待与现价减仓的取舍，依据见下方本轮变化和方案比较。
        </div>
      </div>
      <section v-if="advice.reassessment" class="live-quote live-reassessment">
        <div class="live-section-heading">
          <strong>本轮变化</strong>
          <span class="sub">{{
            thesisStates[advice.reassessment.thesis_status] || "待复核"
          }}</span>
        </div>
        <p class="live-advice-reason">
          {{ advice.reassessment.change_summary }}
        </p>
        <div class="sub">
          <strong>当前假设：</strong>{{ advice.reassessment.thesis }}
        </div>
        <div class="sub">
          <strong>价位依据：</strong>{{ advice.reassessment.price_basis }}
        </div>
      </section>
      <DownsideReview :items="advice.downside_scenarios" />
      <section class="live-proposals">
        <div class="live-section-heading" style="margin-top: 14px">
          <strong>建议预挂条件单</strong><span class="sub">需你在京东确认</span>
        </div>
        <div v-if="!advice.suggested_orders?.length" class="sub">
          {{
            advice.schema_version >= 3
              ? "本轮不建议新增条件单，原因见方案比较。"
              : "本条历史分析未提供条件单方案。"
          }}
        </div>
        <template v-else>
          <div class="sub">
            以下是挂单方案，尚未提交京东。休市不保证触发或成交。
          </div>
          <div
            v-for="(order, i) in advice.suggested_orders"
            :key="i"
            class="live-quote"
          >
            <div class="live-section-heading">
              <strong
                >{{ proposalNames[order.kind] || "条件单" }} ·
                {{ quantity(order.grams) }}</strong
              >
              <span class="sub">{{ proposalState(order) }}</span>
            </div>
            <div class="live-advice-reason">
              {{
                order.kind === "take_profit_stop_loss"
                  ? "止盈 ≥"
                  : order.kind === "limit_buy"
                    ? "买入价 ≤"
                    : "卖出价 ≥"
              }}
              {{ num(order.price_cny) }} 元/克
              <template v-if="order.kind === 'take_profit_stop_loss'">
                · 止损 ≤ {{ num(order.loss_price_cny) }} 元/克</template
              >
            </div>
            <div class="sub">
              条件单截止 {{ fmtTs(order.expires_at) }} · 北京时间
            </div>
            <div class="live-advice-reason">{{ order.reason }}</div>
            <OrderImpact :effects="order.account_effects" :kind="order.kind" />
            <div
              v-for="check in order.checks || []"
              :key="check"
              class="live-advice-check"
            >
              需核对：{{ check }}
            </div>
          </div>
          <div class="sub">
            请在建议有效期内确认是否挂单；挂出后按京东条件单自身期限生效。
          </div>
        </template>
      </section>
    </template>
    <div
      v-if="
        advice.schema_version >= 2 || Array.isArray(advice.order_management)
      "
      class="live-structured"
    >
      <div class="live-section-heading" style="margin-top: 12px">
        <strong>挂单处理建议</strong><span class="sub">撤改需你在京东操作</span>
      </div>
      <div v-if="!advice.order_management?.length" class="sub">
        本轮没有挂单处理建议
      </div>
      <div
        v-for="item in advice.order_management || []"
        :key="item.order_key"
        class="live-quote"
      >
        <div class="live-section-heading">
          <strong>{{
            { keep: "保留原单", cancel: "建议撤单", replace: "建议改单" }[
              item.action
            ] || "处理建议待核对"
          }}</strong
          ><span class="sub">{{
            itemState(item) === "current"
              ? item.action === "keep"
                ? "建议保留，尚待成交"
                : "等待你确认处理"
              : states[itemState(item)] ||
                monitorStates[itemState(item)] ||
                "状态待更新"
          }}</span>
        </div>
        <div class="sub">原单：{{ terms(original(item)) }}</div>
        <div v-if="original(item).status === 'partially_filled'" class="sub">
          原单已有部分成交 · 已成交
          {{ quantity(original(item).filled_grams) }} · 未成交
          {{
            original(item).grams != null && original(item).filled_grams != null
              ? quantity(
                  Math.max(
                    0,
                    original(item).grams - original(item).filled_grams,
                  ),
                )
              : "待核对"
          }}
        </div>
        <div class="sub">
          京东原单有效至 {{ fmtTs(original(item).expires_at) }}
        </div>
        <div v-if="item.action === 'replace'" class="live-advice-check">
          修改后：{{
            terms({
              ...item.effective_replacement,
              action: original(item).action,
            })
          }}
          · 有效至 {{ fmtTs(item.effective_replacement?.expires_at) }}
        </div>
        <div class="live-advice-reason">{{ item.reason }}</div>
        <div
          v-for="check in item.checks || []"
          :key="check"
          class="live-advice-check"
        >
          需核对：{{ check }}
        </div>
      </div>
      <div class="live-section-heading" style="margin-top: 14px">
        <strong>价格触发复评</strong>
      </div>
      <div v-if="!advice.review_conditions?.length" class="sub">
        本轮未设置价格复评条件，沿用其他事件和周期调度。
      </div>
      <div v-else class="sub">
        达到条件仅排队重新分析，不自动撤改或下单。条件有效至
        {{ fmtTs(advice.expires_at) }}。
      </div>
      <div
        v-for="(c, i) in advice.review_conditions || []"
        :key="i"
        class="live-quote"
      >
        <div class="live-section-heading">
          <strong
            >{{ account.name }}金价 {{ symbol[c.condition] }}
            {{ num(c.price_cny) }} 元/克</strong
          ><span class="sub">{{
            monitorStates[conditionState(c)] ||
            states[conditionState(c)] ||
            "等待监控同步"
          }}</span>
        </div>
        <div v-if="c.order_key" class="sub">
          {{
            associated(c)
              ? "关联原单：" + terms(associated(c))
              : "关联挂单已不在当前待成交明细中"
          }}
        </div>
        <div class="live-advice-reason">{{ c.reason }}</div>
        <div v-if="c.monitoring?.last_quote_at" class="live-quote-time">
          最近监测 {{ num(c.monitoring.last_price_cny) }} 元/克 ·
          {{ fmtTs(c.monitoring.last_quote_at) }}
        </div>
        <div v-if="c.monitoring?.triggered_at" class="live-quote-time">
          触发时间 {{ fmtTs(c.monitoring.triggered_at) }}
        </div>
      </div>
    </div>
    <div v-else class="live-advice-check">
      本条旧分析未提供结构化挂单处理或价格复评条件，待下一轮更新。
    </div>
    <div
      v-if="advice.execution_progress?.orders?.length"
      class="live-advice-check"
    >
      {{ orderStates[advice.execution_progress.status] || "执行进度待核对" }} ·
      {{
        advice.execution_progress.match === "similar_parameters"
          ? "发现相近挂单，参数有调整"
          : "按参数与时间匹配，仅为关联推断"
      }}
      · {{ quantity(advice.execution_progress.orders[0].grams) }} ·
      {{ num(advice.execution_progress.orders[0].price_cny) }} 元/克
    </div>
    <div v-if="state === 'expired'" class="live-advice-check">
      {{
        ["pending", "partially_filled"].includes(
          advice.execution_progress?.status,
        )
          ? "原建议已到期；京东条件单仍待成交，以条件单自身有效期为准。"
          : "建议有效期已结束；买卖建议会按数据与调度条件排队复评。"
      }}
    </div>
    <div v-if="state === 'account_changed'" class="live-advice-check">
      持仓、资金或条件单已变化，请结合执行进度等待重新评估。
    </div>
    <div v-if="state === 'data_unavailable'" class="live-advice-check">
      账户数据待刷新，暂勿沿用此建议。
    </div>
    <div
      v-for="check in advice.checks || []"
      :key="check"
      class="live-advice-check"
    >
      需核对：{{ check }}
    </div>
    <details v-if="section !== 'orders'" class="more">
      <summary>
        {{ advice.reassessment ? "查看方案比较与风险" : "查看依据与风险" }}
      </summary>
      <p class="live-advice-reason">
        {{ advice.reason || "本轮未提供判断依据" }}
      </p>
      <div class="sub">{{ advice.risk_note }}</div>
    </details>
  </article>
</template>
