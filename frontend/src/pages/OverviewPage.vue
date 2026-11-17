<script setup>
import { computed } from "vue";
import Card from "../components/Card.vue";
import { navigate, selectView, openBank } from "../composables/dashboard";
import {
  num,
  pct,
  rate,
  fmtTs,
  modelNames,
  actions,
  pastDeadline,
  sourceState,
  executionStatus,
} from "../format";
const props = defineProps({ data: Object });
const quote = computed(
  () => props.data.execution?.quote || props.data.execution?.valuation_quote,
);
const quoteState = computed(() => executionStatus(props.data));
const current = (a) =>
  (a.advice_summary || []).filter(
    (v) =>
      (v.action_status || v.status) === "current" &&
      !pastDeadline(v) &&
      a.connected,
  );
</script>
<template>
  <section id="panel-overview" class="page-stack" data-page-ready="true">
    <Card title="京东积存金" :subtitle="quoteState.label"
      ><template #action
        ><button class="link-btn" @click="selectView('market', 'prices')">
          金价走势 →
        </button></template
      >
      <div class="overview-price">
        {{ num(quote?.price) }}<span> 元/克</span>
      </div>
      <div class="sub">
        京东通用参考报价 ·
        {{ fmtTs(quote?.quote_at)
        }}<span v-if="quoteState.note"> · {{ quoteState.note }}</span>
      </div>
    </Card>
    <Card title="真实账户待关注"
      ><template #action
        ><button class="link-btn" @click="selectView('strategies', 'live')">
          真实账户 →
        </button></template
      >
      <div class="grid-2">
        <article
          v-for="a in data.live_readonly || []"
          :key="a.bank"
          class="overview-bank"
        >
          <div class="row">
            <strong class="grow">{{ a.name }}</strong
            ><span class="tag">{{
              a.connected
                ? "已同步"
                : a.status === "login_required"
                  ? "需要登录"
                  : "数据待更新"
            }}</span>
          </div>
          <p class="sub">
            京东已挂条件单
            {{
              a.pending_order_count == null
                ? "未知"
                : a.pending_order_count + " 笔"
            }}
            · {{ fmtTs(a.fetched_at) }}
          </p>
          <div v-for="v in current(a)" :key="v.id" class="sub">
            {{ modelNames[v.analyst] }} · {{ actions[v.action] }}建议 ·
            待确认条件单方案 {{ v.suggested_order_count }} 笔
            <div v-if="v.status === 'review_required'">
              部分建议需核对，详见账户建议。
            </div>
            <div>建议截止 {{ fmtTs(v.expires_at) }}</div>
          </div>
          <div v-if="!current(a).length" class="sub">
            暂无当前有效建议，进入账户查看状态。
          </div>
          <div class="asset-actions">
            <button class="link-btn" @click="openBank(a.bank, 'advice')">
              查看建议 →</button
            ><button class="link-btn" @click="openBank(a.bank, 'orders')">
              已挂条件单 →
            </button>
          </div>
        </article>
      </div></Card
    >
    <Card title="模型运行"
      ><template #action
        ><button class="link-btn" @click="selectView('ai', 'control')">
          模型控制 →
        </button></template
      >
      <div class="grid-2">
        <article
          v-for="m in (data.model_controls || []).filter((c) => c.enabled)"
          :key="m.key"
        >
          <div class="row">
            <strong class="grow">{{ m.name }}</strong
            ><span class="tag">{{
              m.runtime?.running
                ? "分析中"
                : m.runtime?.queued
                  ? "排队中"
                  : "已开启"
            }}</span>
          </div>
          <p class="sub">
            今日分析 {{ m.runs_today || 0 }} / {{ m.daily_cap || 120 }} 轮
          </p>
        </article>
      </div>
      <div class="detail-actions">
        <button class="link-btn" @click="navigate('records')">
          分析记录 →
        </button>
      </div>
    </Card>
    <Card title="近期重要事件"
      ><template #action
        ><button class="link-btn" @click="selectView('market', 'calendar')">
          经济日历 →
        </button></template
      >
      <div v-for="e in data.hard_events_24h || []" :key="e.title + e.time_bj">
        {{ e.time_bj }} {{ e.title }}
      </div>
      <div v-if="!data.hard_events_24h?.length" class="sub">
        未来24小时暂无已识别重大事件
      </div></Card
    >
  </section>
</template>
