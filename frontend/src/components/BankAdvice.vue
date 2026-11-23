<script setup>
import { computed } from "vue";
import LiveRiskSummary from "./LiveRiskSummary.vue";
import DownsideReview from "./DownsideReview.vue";
import PositionPath from "./PositionPath.vue";
import ParticipationReview from "./ParticipationReview.vue";
import OrderImpact from "./OrderImpact.vue";
import {
  actions,
  modelNames,
  quantity,
  num,
  signed,
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
  plan_reviewed: "本计划已复评",
  latched: "持续满足 · 不重复触发",
  price_limit_exceeded: "已超过买价上限 · 本轮放弃",
  pullback_lost: "已跌穿回踩区间 · 本轮放弃",
  market_closed: "休市暂停",
  expired: "已到期",
  account_changed: "账户或委托变化 · 已失效",
  data_unavailable: "等待新鲜报价或挂单明细",
  order_inactive: "目标挂单已结束或待更新",
  review_required: "需核对",
};
const confirmationStages = {
  touched: "已触价 · 等待回升或回踩",
  retest: "已回踩 · 等待收复观察价",
  holding: "已达到观察价 · 正在计时",
  breakout_failed: "突破失守 · 等待再次突破",
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
  partState.value !== "current" && !c.monitoring?.expires_at
    ? partState.value
    : !props.quoteFresh
      ? "data_unavailable"
      : c.monitoring?.status;
const conditionHeading = (c) => {
  if (c.bank_breakout?.earlier_high)
    return `更早高位 ${num(c.price_cny)} 元/克`;
  if (c.bank_breakout) return `突破 ${num(c.price_cny)} 元/克后观察`;
  const floor = c.buy_review?.zone?.floor_cny;
  if (floor != null) {
    const label =
      { deep: "深回调区间", shallow: "浅回调区间" }[c.pullback_depth] ||
      "回调区间";
    return c.condition === "at_or_above"
      ? `突破 ${num(c.price_cny)} 元/克后等回踩`
      : `${label} ${num(floor)}–${num(c.price_cny)} 元/克`;
  }
  return `${props.account.name}金价 ${symbol[c.condition]} ${num(c.price_cny)} 元/克`;
};
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
            : advice.position_path?.state === "boundary_reached" &&
                advice.action === "buy"
              ? "买入已暂停 · 待复评"
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
          本轮重点复核旧价位，以及新增一笔、维持现仓与减仓的取舍，依据见下方本轮变化和方案比较。
        </div>
      </div>
      <ParticipationReview
        :review="
          advice.participation_review ||
          advice.input_strategy_review?.participation_review
        "
        :snapshot="!advice.participation_review"
      />
      <section
        v-if="advice.reassessment && !advice.presentation"
        class="live-quote live-reassessment"
      >
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
      <PositionPath
        :path="advice.position_path"
        :steps="advice.presentation?.plan_steps"
        :note="advice.presentation?.execution_note"
        :active="partState === 'current' && quoteFresh"
      />
      <section
        v-if="advice.presentation?.buy_sizing?.length"
        class="live-quote"
        data-buy-sizing
      >
        <div class="live-section-heading">
          <strong>买入克数测算</strong><span class="sub">按分析时数据</span>
        </div>
        <div
          v-for="(size, i) in advice.presentation.buy_sizing"
          :key="i"
          class="sub"
        >
          <strong>{{
            {
              immediate: "本轮买入",
              recommend: "限价买入建议",
              needs_review: "回调复评情景",
            }[size.kind]
          }}</strong>
          · 按 {{ num(size.entry_price_cny) }} 元/克
          <template v-if="size.suggested_grams != null">
            · 建议 {{ num(size.suggested_grams, 4) }} 克</template
          >
          <div v-if="size.target_add_grams != null">
            达到 {{ num(size.target_position_pct) }}% 总仓位约需追加
            {{ num(size.target_add_grams) }} 克
            <template v-if="size.suggested_grams == null">
              · 克数待复评后确定</template
            >
            <template v-if="size.remaining_after_batch_grams > 0">
              · 本批成交后距目标约
              {{ num(size.remaining_after_batch_grams) }} 克
            </template>
          </div>
          <div
            v-if="
              size.target_fits_cash === false || size.target_fits_risk === false
            "
          >
            按同一价格一次补足目标会超出{{
              size.target_fits_cash === false && size.target_fits_risk === false
                ? "可用现金和风险预算参考"
                : size.target_fits_cash === false
                  ? "可用现金"
                  : "风险预算参考"
            }}。
          </div>
          <div v-if="size.spending_cny != null">
            {{ size.suggested_grams == null ? "按目标估算" : "按建议估算" }}投入
            {{ num(size.spending_cny) }} 元
            <template v-if="size.spending_nav_pct != null">
              · 占净值 {{ num(size.spending_nav_pct) }}%</template
            >
            <template v-if="size.position_after_pct != null">
              · 成交后总仓位 {{ num(size.position_after_pct) }}%</template
            >
          </div>
          <div
            v-if="size.cash_cap_grams != null || size.risk_cap_grams != null"
          >
            <template v-if="size.cash_cap_grams != null"
              >扣现有买单后现金可买 {{ num(size.cash_cap_grams) }} 克</template
            >
            <template
              v-if="size.cash_cap_grams != null && size.risk_cap_grams != null"
            >
              ·
            </template>
            <template v-if="size.risk_cap_grams != null"
              >风险预算上限参考 {{ num(size.risk_cap_grams) }} 克</template
            >
          </div>
          <div v-if="size.account_at_exit_change_cny != null">
            到 {{ num(size.exit_price_cny) }} 元/克时，现仓加本批的扣费净值变化
            {{ signed(size.account_at_exit_change_cny) }} 元（{{
              signed(size.account_at_exit_change_pct, 3)
            }}%）
          </div>
          <template v-if="size.combined_buys">
            <div>
              若本批和其他买单都成交，共买入
              {{ num(size.combined_buys.total_buy_grams) }} 克， 占用
              {{ num(size.combined_buys.total_spending_cny) }} 元
              <template v-if="size.combined_buys.position_after_pct != null">
                · 成交后总仓位 {{ num(size.combined_buys.position_after_pct) }}%
              </template>
              <template
                v-if="size.combined_buys.remaining_after_batch_grams > 0"
              >
                · 距目标约
                {{ num(size.combined_buys.remaining_after_batch_grams) }} 克
              </template>
              <template v-if="size.combined_buys.cash_covers_all === false">
                · 现金不足以覆盖</template
              >
            </div>
            <div
              v-if="
                size.combined_buys.cash_cap_grams != null &&
                size.combined_buys.cash_cap_grams !== size.cash_cap_grams
              "
            >
              为其他买单留足资金后，本批现金上限
              {{ num(size.combined_buys.cash_cap_grams) }} 克
            </div>
            <div v-if="size.combined_buys.account_at_exit_change_cny != null">
              同一退出价的扣费净值变化
              {{ signed(size.combined_buys.account_at_exit_change_cny) }} 元
              （{{
                signed(size.combined_buys.account_at_exit_change_pct, 3)
              }}%）
              <template v-if="size.combined_buys.risk_cap_grams != null">
                · 计入其他买单后，本批风险上限参考
                {{ num(size.combined_buys.risk_cap_grams) }} 克
              </template>
            </div>
          </template>
        </div>
      </section>
      <DownsideReview :items="advice.downside_scenarios" />
      <section class="live-proposals">
        <div class="live-quote" data-protection-status>
          <strong>当前保护查询</strong>
          <p class="sub" v-if="account.protection_state?.status === 'none'">
            接口未发现已挂组合止盈止损单；建议保护价不代表已生效。
          </p>
          <p
            class="sub"
            v-else-if="account.protection_state?.status === 'present'"
          >
            已读取
            {{ account.protection_state.count }}
            笔组合止盈止损单，不代表覆盖全部持仓；与建议的关联仍需核对。
          </p>
          <p class="sub" v-else>挂单明细不足以确认保护状态，请在京东核对。</p>
          <div v-if="advice.input_protection" class="sub">
            分析时：{{
              {
                none: "未发现已挂组合保护",
                present: "已读取组合保护挂单",
                unknown: "保护状态待确认",
              }[advice.input_protection.status]
            }}。
          </div>
        </div>
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
            以下是AI建议方案。是否已挂出见各单状态，实际委托以京东为准。
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
      <div v-else class="sub">达到观察条件会排队重新分析。</div>
      <div
        v-for="(c, i) in advice.review_conditions || []"
        :key="i"
        class="live-quote"
      >
        <div class="live-section-heading">
          <strong>{{ conditionHeading(c) }}</strong
          ><span class="sub">{{
            (conditionState(c) === "monitoring"
              ? confirmationStages[c.monitoring?.confirmation_stage]
              : null) ||
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
        <div v-if="c.monitoring?.renewed_pullback" class="sub">
          本行近期回补报价已提高，触价后重新评估。
        </div>
        <div v-if="c.bank_breakout" class="sub">
          突破后至少
          {{ c.monitoring?.continuation_min_quotes ?? 3 }} 笔买价在高位上方，
          跨满
          {{ c.monitoring?.continuation_minutes ?? 3 }}
          分钟且刷新突破后的高点时复评。
          <template v-if="!c.bank_breakout.earlier_high">
            突破后回踩 {{ num(c.price_cny) }}–{{
              num(c.bank_breakout.retest_ceiling_cny)
            }}
            元/克，守住 {{ num(c.price_cny) }} 元/克满
            {{ c.bank_breakout.hold_minutes }} 分钟后复评。
          </template>
        </div>
        <div v-if="c.buy_review?.zone && !c.checks?.length" class="sub">
          <template v-if="c.condition === 'at_or_above'">
            回踩观察区 {{ num(c.buy_review.zone.floor_cny) }}–{{
              num(c.price_cny)
            }}
            元/克。
          </template>
          买价守住 {{ num(c.buy_review.recovery_price_cny) }} 元/克满
          {{ c.buy_review.hold_minutes }} 分钟、且不超过
          {{ num(c.buy_review.max_buy_price_cny) }} 元/克，再评估{{
            c.buy_review.grams == null
              ? "是否加仓及数量"
              : "是否买入 " + quantity(c.buy_review.grams)
          }}。跌穿区间下缘则放弃。
          {{ c.buy_review.zone.basis }}
        </div>
        <div v-else-if="c.buy_review && !c.checks?.length" class="sub">
          {{
            c.condition === "at_or_above" ? "突破后等回踩" : "回调触价后等回升"
          }}， 买价守住 {{ num(c.buy_review.recovery_price_cny) }} 元/克满
          {{ c.buy_review.hold_minutes }} 分钟，再评估{{
            c.buy_review.grams == null
              ? "是否加仓及数量"
              : "是否买入 " + quantity(c.buy_review.grams)
          }}。触价后若超过
          {{ num(c.buy_review.max_buy_price_cny) }} 元/克，本轮放弃。
        </div>
        <div v-if="c.monitoring?.last_quote_at" class="live-quote-time">
          最近监测 {{ num(c.monitoring.last_price_cny) }} 元/克 ·
          {{ fmtTs(c.monitoring.last_quote_at) }}
        </div>
        <div v-if="c.monitoring?.triggered_at" class="live-quote-time">
          {{
            c.monitoring.confirmation_mode === "continuation"
              ? "持续走高触发"
              : "触发时间"
          }}
          {{ fmtTs(c.monitoring.triggered_at) }}
        </div>
        <div class="sub">
          观察截止 {{ fmtTs(c.monitoring?.expires_at || advice.expires_at) }}
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
    <section v-if="section !== 'orders'" class="live-quote" data-advice-basis>
      <strong>关键依据</strong>
      <div
        v-for="(item, index) in advice.presentation?.basis || [
          {
            label: '操作与数量说明',
            text: advice.reason || '本轮未提供判断依据',
          },
        ]"
        :key="index"
      >
        <p class="sub">
          <strong>{{ item.label }}</strong>
        </p>
        <p class="live-advice-reason">{{ item.text }}</p>
      </div>
    </section>
  </article>
</template>
