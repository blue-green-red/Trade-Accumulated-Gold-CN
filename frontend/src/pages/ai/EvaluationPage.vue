<script setup>
import {
  computed,
  ref,
  defineAsyncComponent,
  onMounted,
  onActivated,
} from "vue";
import Card from "../../components/Card.vue";
import { navigate } from "../../composables/dashboard";
import { num, fmtTs } from "../../format";
const props = defineProps({ data: Object });
const analysts = computed(() => props.data.ai?.analysts || []);
const showScore = ref(false);
const ForecastCard = defineAsyncComponent(
  () => import("../../components/ForecastCard.vue"),
);
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div class="page-stack">
    <Card
      v-for="v in analysts"
      :key="v.analyst"
      :title="v.name + ' · 分析效率'"
      :subtitle="fmtTs(v.ts)"
    >
      <details v-if="v._performance" class="more" open>
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
    </Card>
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
            国际黄金期货历史（开/收盘）
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
  </div>
</template>
