<script setup>
import { computed } from "vue";
import { clock } from "../clock";
import Card from "./Card.vue";

import { pending, actionErrors, refreshLive } from "../composables/dashboard";
const props = defineProps({ data: Object });
const rows = computed(() => props.data.live_readonly || []);
const recent = (v) =>
  v &&
  clock.value - new Date(v).getTime() >= 0 &&
  clock.value - new Date(v).getTime() < 180000;
const busy = computed(
  () =>
    pending["live-refresh"] ||
    (["queued", "running"].includes(props.data.live_readonly_refresh?.status) &&
      recent(props.data.live_readonly_refresh?.requested_at)) ||
    (props.data.live_readonly_sync?.status === "running" &&
      recent(props.data.live_readonly_sync?.at)),
);
const needsLogin = computed(
  () =>
    rows.value.some((a) => a.status === "login_required") ||
    props.data.live_readonly_sync?.status === "login_required",
);
</script>
<template>
  <Card title="京东真实账户" subtitle="两家资金独立 · 只读查询 · 人工执行"
    ><div class="live-toolbar">
      <div>
        <div class="sub">
          两家资金独立 · 与模拟账户分开 · 建议由你在官方页面确认
        </div>
        <div class="sub" role="status">
          {{
            busy
              ? "正在更新账户数据…"
              : data.live_readonly_sync?.status === "waiting_for_resources"
                ? "等待模型分析结束后恢复采集"
                : "报价约1分钟 · 账户/委托约5分钟 · 手动交易后可立即刷新"
          }}
        </div>
        <div
          v-if="data.live_readonly_refresh?.status === 'completed'"
          class="sub"
        >
          上次手动刷新已完成
        </div>
      </div>
      <button
        class="live-refresh"
        :disabled="busy"
        :aria-label="busy ? '更新中…' : '刷新真实账户'"
        @click="refreshLive"
      >
        {{ busy ? "更新中…" : "刷新真实账户" }}
      </button>
    </div>
    <div v-if="actionErrors['live-refresh']" class="notice warn" role="alert">
      {{ actionErrors["live-refresh"] }}
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
