<script setup>
import {
  computed,
  ref,
  defineAsyncComponent,
  onMounted,
  onActivated,
} from "vue";
import Card from "../../components/Card.vue";
import { navigate, selectView } from "../../composables/dashboard";
import { rate, fmtTs, bias, actions, safeUrl } from "../../format";
const props = defineProps({ data: Object });
const analysts = computed(() => props.data.ai?.analysts || []);
function live() {
  selectView("strategies", "live");
}
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div class="page-stack">
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
        <div class="chips">
          <span
            v-for="(count, action) in v.action_counts"
            :key="action"
            class="chip"
            >{{ actions[action] || action }} {{ count }} 户</span
          >
        </div>
        <div class="asset-actions">
          <button
            class="link-btn"
            @click="selectView('strategies', 'simulation')"
          >
            查看模拟账户决策 →</button
          ><button v-if="v.live_advice_count" class="link-btn" @click="live">
            查看真实账户建议 →
          </button>
        </div></Card
      >
    </div>
    <Card v-else title="最新分析"
      ><div class="empty">
        {{
          data.ai?.enabled
            ? "等待首次分析，满足价格变化、事件或兜底条件时排队"
            : "分析后端尚未就绪，请到模型控制查看状态"
        }}
      </div></Card
    >
  </div>
</template>
