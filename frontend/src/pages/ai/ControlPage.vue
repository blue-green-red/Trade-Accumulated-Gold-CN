<script setup>
import {
  computed,
  ref,
  defineAsyncComponent,
  onMounted,
  onActivated,
} from "vue";
import Card from "../../components/Card.vue";
import {
  pending,
  actionErrors,
  toggleModel,
  manualAnalysis,
} from "../../composables/dashboard";
const props = defineProps({ data: Object });
const manualPending = (c) => ["queued", "running"].includes(c.manual?.status);
const manualDisabled = (c) =>
  pending["manual:" + c.key] ||
  c.available !== true ||
  manualPending(c) ||
  (c.runs_today || 0) >= (c.daily_cap || 120) - (c.urgent_reserve || 0);
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div class="page-stack">
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
    <Card title="分析节奏与执行规则">
      <p class="sub">
        周期复查：{{
          data.cadence?.review_minutes > 0
            ? data.cadence.review_minutes + " 分钟后，"
            : "无最小时间间隔，"
        }}国际金价或任一真实账户银行价，较各自上次分析输入变化≥{{
          data.cadence?.min_move_pct
        }}%可触发同一轮复查；最长{{
          (data.cadence?.max_idle_minutes || 240) / 60
        }}小时兜底。重大事件另外排队，执行器每分钟检查。
      </p>
      <p class="sub">
        模拟盘按数日至一两周波段运行；真实账户建议在账户页查看。
      </p>
      <details class="more" data-detail-key="trading-policy">
        <summary>风险预算与执行规则</summary>
        <p class="sub">
          三档风险：单次计划损失1%/2%/4%，阶段回撤线5%/10%/20%，触线暂停新增买入。计划风险参考不自动止损，实际损失可能超预算。短线按数日至一两周运行，每周约两次买卖操作为节奏参考，不要求凑次数。AI
          自主决定数量与保护价，可设置、放宽或撤销止损止盈；不设置时不按固定亏损自动平仓。账户间不转账；初始满仓仅在开账时买入一次。未成交委托到期撤销，已有持仓保护继续有效。
        </p>
      </details></Card
    >
  </div>
</template>
