<script setup>
import { computed, defineAsyncComponent } from "vue";
import Card from "./Card.vue";
const BankAdvice = defineAsyncComponent(() => import("./BankAdvice.vue"));
import { pending, actionErrors, refreshLive } from "../composables/dashboard";
import {
  num,
  money,
  quantity,
  fmtTs,
  modelNames,
  actions,
  safeUrl,
} from "../format";
import { clock } from "../clock";
const props = defineProps({
  data: Object,
  section: { type: String, default: "holdings" },
});
const rows = computed(() => props.data.live_readonly || []);
const states = {
  fresh: "已同步",
  stale: "快照已过期",
  login_required: "需要京东登录",
  unavailable: "查询失败",
  not_checked: "待首次同步",
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
const fresh = computed(() => rows.value.filter((a) => a.connected).length);
const recent = (v) =>
  v &&
  Date.now() - new Date(v).getTime() >= 0 &&
  Date.now() - new Date(v).getTime() < 180000;
const busy = computed(
  () =>
    pending["live-refresh"] ||
    (["queued", "running"].includes(props.data.live_readonly_refresh?.status) &&
      recent(props.data.live_readonly_refresh?.requested_at)) ||
    (props.data.live_readonly_sync?.status === "running" &&
      recent(props.data.live_readonly_sync?.at)),
);
const quoteCurrent = (a) =>
  a.connected &&
  a.quote?.status !== "historical" &&
  a.quote?.quoted_at &&
  clock.value - new Date(a.quote.quoted_at).getTime() >= -30000 &&
  clock.value - new Date(a.quote.quoted_at).getTime() <= 180000;
const needsLogin = computed(
  () =>
    rows.value.some((a) => a.status === "login_required") ||
    props.data.live_readonly_sync?.status === "login_required",
);
const profile = computed(
  () => rows.value.find((a) => a.risk_profile)?.risk_profile,
);
const cashSource = (a) =>
  a.cash_source === "jd_electronic_account_api"
    ? "银行黄金电子户接口 · 不含绑定银行卡"
    : a.cash_source === "user_declared_full_position"
      ? "用户确认当前满仓 · " + fmtTs(a.cash_declared_at)
      : a.cash_source === "user_declaration_invalidated"
        ? "账户已变化 · 原满仓声明失效"
        : "未取得可用资金，保留未知";
</script>
<template>
  <Card
    title="京东真实账户 · 只读接入"
    :subtitle="`${fresh}/${rows.length} 户已同步 · 人工执行`"
    class="live-panel"
  >
    <div class="live-bank-grid bank-detail-grid">
      <article
        v-for="a in rows"
        :key="a.bank"
        class="live-bank"
        :style="{
          '--live-accent':
            a.bank === 'minsheng' ? 'var(--series-1)' : 'var(--brand)',
        }"
      >
        <header class="live-bank-head">
          <span class="live-bank-mark" aria-hidden="true">{{
            a.bank === "minsheng" ? "民" : "浙"
          }}</span>
          <div class="live-bank-name">
            <h3>{{ a.name }}</h3>
            <div class="sub">独立银行账户</div>
          </div>
          <span class="live-state" :class="a.connected ? 'fresh' : 'warn'">{{
            states[a.status] || "待同步"
          }}</span>
        </header>
        <div class="live-bank-body">
          <div class="live-snapshot">
            账户采集 {{ fmtTs(a.fetched_at) }} · 北京时间
          </div>
          <div v-if="!a.connected" class="live-stale-note">
            {{
              a.fetched_at
                ? "当前显示上次成功快照，刷新成功前请勿沿用旧建议。"
                : "账户尚未接通，持仓与资金保持未知。"
            }}
          </div>
          <template v-if="section === 'holdings'">
            <div class="live-gold-label">黄金持仓</div>
            <div class="live-gold-value">{{ quantity(a.total_grams) }}</div>
            <div class="live-holding-meta">
              <span>可卖 {{ quantity(a.sellable_grams) }}</span
              ><span>冻结 {{ quantity(a.frozen_grams) }}</span>
            </div>
            <div class="live-holding-meta">
              成本均价
              {{
                a.average_cost_cny == null
                  ? "未知"
                  : num(a.average_cost_cny) + " 元/克"
              }}
            </div>
            <div class="live-balances">
              <div class="live-metric">
                <div class="live-metric-label">
                  {{
                    a.cash_source === "user_declared_full_position"
                      ? "可用现金 · 用户声明"
                      : "电子户可用现金"
                  }}
                </div>
                <div class="live-metric-value">
                  {{ a.cash_cny == null ? "未知" : money(a.cash_cny) }}
                </div>
                <div class="live-metric-note">{{ cashSource(a) }}</div>
              </div>
              <div class="live-metric">
                <div class="live-metric-label">浮动盈亏估算</div>
                <div
                  class="live-metric-value"
                  :class="
                    a.holding_pnl_cny > 0
                      ? 'live-pnl-positive'
                      : a.holding_pnl_cny < 0
                        ? 'live-pnl-negative'
                        : ''
                  "
                >
                  {{ money(a.holding_pnl_cny, true) }}
                </div>
                <div class="live-metric-note">
                  未扣卖费 ·
                  {{
                    quoteCurrent(a) ? "按银行金价" : "按历史金价估算或缺报价"
                  }}
                </div>
              </div>
            </div>
            <div class="live-quote">
              <div class="live-section-heading">
                <span>银行金价 · 元/克</span
                ><span
                  class="live-state"
                  :class="quoteCurrent(a) ? 'fresh' : 'warn'"
                  >{{
                    quoteCurrent(a)
                      ? "3分钟内报价"
                      : a.quote
                        ? "历史报价"
                        : "报价未知"
                  }}</span
                >
              </div>
              <div class="live-quote-price">
                <strong>{{
                  num(a.quote?.sell_price_cny ?? a.quote?.buy_price_cny)
                }}</strong>
                <span class="sub">买卖同价</span>
              </div>
              <div class="live-quote-time">
                {{
                  a.quote
                    ? "报价时刻 " + fmtTs(a.quote.quoted_at) + " · 北京时间"
                    : "未取得该银行报价，不使用其他银行价格替代"
                }}
              </div>
            </div> </template
          ><template v-if="section === 'orders'">
            <div class="live-orders">
              <span
                >待处理条件单
                {{
                  a.pending_order_count == null
                    ? "未知"
                    : a.pending_order_count + " 笔"
                }}</span
              ><span class="sub">{{
                a.pending_orders?.complete
                  ? "已取得待成交明细"
                  : "待成交明细尚不完整"
              }}</span>
            </div>
            <div v-if="!a.pending_orders?.records?.length" class="sub">
              {{
                a.pending_orders?.complete
                  ? "当前没有待成交条件单。"
                  : "尚未取得完整待成交明细，数量保持未知；请更新账户后核对。"
              }}
            </div>
            <details v-if="a.pending_orders?.records?.length" class="more" open>
              <summary>
                京东条件单明细 · {{ a.pending_orders.records.length }} 笔
              </summary>
              <ul class="list">
                <li v-for="o in a.pending_orders.records" :key="o.order_key">
                  <strong
                    >{{ actions[o.action] || "方向待核对" }}
                    {{ quantity(o.grams) }} ·
                    {{
                      o.status === "pending" &&
                      o.expires_at &&
                      new Date(o.expires_at).getTime() <= clock
                        ? "有效期已过 · 状态待同步"
                        : orderStates[o.status] || "状态待核对"
                    }}</strong
                  >
                  <div class="sub">
                    {{
                      o.price_condition === "at_or_above"
                        ? "≥"
                        : o.price_condition === "at_or_below"
                          ? "≤"
                          : "参考"
                    }}
                    {{ num(o.price_cny) }} 元/克<span v-if="o.loss_price_cny">
                      · 止损 ≤ {{ num(o.loss_price_cny) }}</span
                    >
                  </div>
                  <div class="sub">
                    条件单有效至 {{ fmtTs(o.expires_at) }} · 同步
                    {{ fmtTs(a.component_times?.pending_orders) }}
                  </div>
                </li>
              </ul>
            </details> </template
          ><template v-if="['advice', 'orders'].includes(section)">
            <div
              v-for="r in a.review_status || []"
              :key="r.analyst"
              class="live-advice-check"
            >
              {{ modelNames[r.analyst] }} ·
              {{ r.status === "running" ? "正在重新分析" : "待重新评估" }}：{{
                r.reasons.join("；")
              }}
            </div>
            <section
              class="live-advice-section"
              :aria-label="a.name + ' 人工执行建议'"
            >
              <div class="live-section-heading">
                <h4 class="live-section-title">
                  {{
                    section === "orders" ? "挂单处理与价格复评" : "AI 账户建议"
                  }}
                </h4>
                <span class="sub">由你确认执行</span>
              </div>
              <div v-if="!a.advice?.length" class="live-advice-empty">
                <strong>尚未生成真实账户建议</strong>
                <div class="sub">
                  {{
                    a.connected
                      ? "等待下一轮已开启模型的分析。未生成建议不代表持有或观望。"
                      : "先恢复账户数据，再等待已开启模型分析。"
                  }}
                </div>
              </div>
              <BankAdvice
                v-for="v in a.advice || []"
                :key="v.id"
                :account="a"
                :advice="v"
                :quote-fresh="!!quoteCurrent(a)"
                :section="section"
              />
            </section>
          </template>
          <div
            v-if="section === 'records' || section === 'holdings'"
            class="live-history"
          >
            <details
              v-if="section === 'records' && a.advice_history?.length"
              class="more"
            >
              <summary>最近建议与执行进度</summary>
              <ul class="list">
                <li v-for="v in a.advice_history" :key="v.id">
                  <strong
                    >{{ modelNames[v.analyst] }} · {{ actions[v.action] }}建议 ·
                    #{{ v.id }}</strong
                  >
                  <div class="sub">
                    {{ fmtTs(v.created_at) }} ·
                    {{ v.grams == null ? "无买卖数量" : quantity(v.grams) }} ·
                    {{
                      v.price_cny == null
                        ? "未设价格"
                        : num(v.price_cny) + " 元/克"
                    }}
                  </div>
                  <div v-if="!v.suggested_orders?.length" class="sub">
                    {{
                      orderStates[v.execution_progress?.status] ||
                      (v.execution_progress?.status === "ambiguous"
                        ? "多笔相近委托，待核对"
                        : "未关联到条件单；不代表未手动成交")
                    }}{{
                      v.execution_progress?.match === "similar_parameters"
                        ? " · 参数有调整"
                        : ""
                    }}
                  </div>
                  <div
                    v-for="(order, i) in v.suggested_orders || []"
                    :key="i"
                    class="sub"
                  >
                    条件单方案：{{
                      order.kind === "limit_buy" ? "买入" : "卖出"
                    }}
                    {{ quantity(order.grams) }} ·
                    {{ num(order.price_cny) }} 元/克 ·
                    {{
                      orderStates[order.execution_progress?.status]
                        ? orderStates[order.execution_progress.status] +
                          "（关联推断）"
                        : "未关联到已挂单"
                    }}
                  </div>
                </li>
              </ul>
            </details>
            <details
              v-if="
                section === 'records' &&
                a.order_history?.some((o) => o.status !== 'pending')
              "
              class="more"
            >
              <summary>条件单状态记录</summary>
              <ul class="list">
                <li
                  v-for="o in a.order_history
                    .filter((o) => o.status !== 'pending')
                    .slice(0, 8)"
                  :key="o.order_key"
                >
                  <strong
                    >{{
                      o.status === "pending" &&
                      o.expires_at &&
                      new Date(o.expires_at).getTime() <= clock
                        ? "有效期已过 · 状态待同步"
                        : orderStates[o.status] || "状态待核对"
                    }}
                    · {{ quantity(o.grams) }}</strong
                  >
                  <div class="sub">
                    {{ num(o.price_cny) }} 元/克 · 创建
                    {{ fmtTs(o.created_at) }}
                  </div>
                </li>
              </ul>
            </details>
            <details
              v-for="[kind, title, limit] in [
                ['holding_details', '持仓批次', 3],
                ...(section === 'records'
                  ? [['closed_positions', '已清仓记录', 5]]
                  : []),
              ]"
              :key="kind"
              class="more"
            >
              <summary>
                {{ title
                }}<span class="live-history-count">{{
                  a[kind]
                    ? "本页 " +
                      a[kind].records.length +
                      " / 共 " +
                      (a[kind].total_count ?? "未知") +
                      " 条"
                    : "未取得"
                }}</span>
              </summary>
              <div class="live-history-note">
                {{
                  a[kind]
                    ? "仅展示首页最多20条；模型最多选取前" +
                      limit +
                      "条作为参考，不代表完整成交历史。"
                    : "本次未取得记录，数量保持未知。"
                }}
              </div>
              <ul v-if="a[kind]?.records?.length" class="list">
                <li v-for="(r, i) in a[kind].records" :key="i">
                  <div class="live-history-row">
                    <span>{{ fmtTs(r.time) }}</span
                    ><strong>{{ quantity(r.orderGram) }}</strong>
                  </div>
                  <div class="sub">
                    买入 {{ num(r.buyPrice) }} 元/克<span
                      v-if="r.sellAvgPrice != null"
                    >
                      · 卖出 {{ num(r.sellAvgPrice) }} 元/克</span
                    ><span v-if="r.totalGram != null">
                      · 剩余 {{ quantity(r.totalGram) }}</span
                    >
                  </div>
                </li>
              </ul>
              <div v-else class="live-history-note">
                {{ a[kind] ? "接口本页未返回记录" : "刷新账户后重试" }}
              </div>
            </details>
            <div v-if="a.recent_trade" class="live-history-note">
              最近一笔 · {{ fmtTs(a.recent_trade.transactionTime) }} ·
              {{ a.recent_trade.trade_label || "类型待核对" }} ·
              {{ quantity(a.recent_trade.orderGram) }} ·
              {{ money(a.recent_trade.orderAmount) }}
            </div>
          </div>
          <div class="live-bank-footer">
            <span>只读查询 · 真实资产</span
            ><a
              :href="safeUrl(a.official_url)"
              target="_blank"
              rel="noopener noreferrer"
              class="link-btn"
              >打开京东官方页面 ↗</a
            >
          </div>
        </div>
      </article>
    </div>
    <div
      v-if="needsLogin"
      class="notice warn"
      role="status"
      style="margin-top: 16px"
    >
      <strong>需要重新登录京东</strong>
      <div class="sub">
        京东会话已失效或尚未建立。需要登录时再开启ECS临时远程浏览器，由你完成登录；两家银行共用会话，登录后自动恢复采集。本看板不接收京东密码或验证码。
      </div>
    </div></Card
  >
</template>
