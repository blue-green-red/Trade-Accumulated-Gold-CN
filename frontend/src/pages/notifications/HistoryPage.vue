<script setup>
import { computed, ref, onMounted, onActivated } from "vue";
import { navigate } from "../../composables/dashboard";
import Card from "../../components/Card.vue";
import { displayTitle, fmtTs } from "../../format";
const props = defineProps({ data: Object });
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
const filter = ref("all");
const messages = computed(() => props.data.notifications?.messages || []);
const needsAttention = (r) =>
  ["failed", "uncertain", "retry", "sending", "pending"].includes(r.status);
const rows = computed(() =>
  messages.value.filter((r) => filter.value === "all" || needsAttention(r)),
);
const states = {
  accepted: "服务已受理",
  failed: "发送失败",
  uncertain: "受理结果待核对",
  not_sent: "未发送",
  retry: "等待重试",
  pending: "待发送",
  sending: "正在发送",
  cancelled: "已停止发送",
};
</script>
<template>
  <div class="page-stack">
    <button class="link-btn back-link" @click="navigate('me')">
      ‹ 返回我的
    </button>
    <Card
      title="微信发送记录"
      :subtitle="`最近 ${messages.length} 条消息 · 同一消息重试合并展示`"
    >
      <p class="sub">
        服务受理不代表手机已收到或已读。关联事件在消息内展开，不重复计为微信消息。
      </p>
      <div class="chips">
        <button
          v-for="[key, label] in [
            ['all', '全部消息'],
            ['attention', '待处理 / 失败'],
          ]"
          :key="key"
          class="filter-btn"
          :aria-pressed="filter === key"
          @click="filter = key"
        >
          {{ label
          }}<span v-if="key === 'attention'">
            {{ messages.filter(needsAttention).length }}</span
          >
        </button>
      </div>
      <ul v-if="rows.length" class="list">
        <li v-for="r in rows" :key="r.id">
          <div class="row">
            <strong class="grow">{{ displayTitle(r.title) }}</strong
            ><span class="tag" :class="needsAttention(r) ? 'bad' : ''">{{
              states[r.status] || "状态待核对"
            }}</span>
          </div>
          <div class="sub">
            {{ fmtTs(r.ts) }} ·
            {{
              r.category === "real_advice"
                ? "真实账户建议"
                : r.category === "pending"
                  ? "待发送事件"
                  : "分析 / 模拟操盘"
            }}
          </div>
          <div v-if="r.next_attempt_at" class="sub">
            下次尝试 {{ fmtTs(r.next_attempt_at) }}
          </div>
          <details class="more">
            <summary>
              关联事件 {{ r.events?.length || 0 }} · 发送尝试
              {{ r.attempts?.length || r.attempt_count || 0 }}
            </summary>
            <ul class="list">
              <li v-for="e in r.events" :key="e.key">
                {{ displayTitle(e.title) }}
                <div class="sub">{{ fmtTs(e.ts) }}</div>
              </li>
              <li v-for="a in r.attempts" :key="a.id">
                <div>
                  {{ fmtTs(a.ts) }} · {{ states[a.status] || a.status }} ·
                  {{ a.channel }}
                </div>
                <div v-if="a.provider_id" class="sub">
                  消息流水号 <code>{{ a.provider_id }}</code>
                </div>
                <div v-if="a.provider_code != null" class="sub">
                  渠道状态 {{ a.provider_code }}
                </div>
              </li>
            </ul>
          </details>
        </li>
      </ul>
      <div v-else class="empty">暂无匹配的发送记录</div></Card
    >
  </div>
</template>
