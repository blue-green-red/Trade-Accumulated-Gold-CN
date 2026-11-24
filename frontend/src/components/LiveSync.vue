<script setup>
import { computed } from "vue";
import { clock } from "../clock";
import Card from "./Card.vue";

import { pending, actionErrors, refreshLive } from "../composables/dashboard";
const props = defineProps({ data: Object });
const rows = computed(() => props.data.live_readonly || []);
const desktop = computed(() => props.data.live_readonly_sync?.source === "desktop");
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
  <Card title="京东真实账户" subtitle="账户资金独立 · 只读查询 · 人工执行"
    ><div class="live-toolbar">
      <div>
        <div class="sub">
          {{ desktop ? "京东金融桌面版 · 只读同步" : "账户只读同步 · 买卖与挂单由你在官方页面确认" }}
        </div>
        <div class="sub" role="status">
          {{
            busy
              ? "正在更新账户数据…"
              : data.live_readonly_sync?.status === "waiting_for_resources"
                ? "等待模型分析结束后恢复采集"
                : desktop
                  ? "请在京东金融打开积存金页面。看板仅读取页面上能看到的数据。"
                  : "报价约1分钟 · 账户/委托约5分钟 · 手动交易后可立即刷新"
          }}
        </div>
        <div
          v-if="data.live_readonly_refresh?.status === 'completed'"
          class="sub"
        >
          {{
            data.live_readonly_refresh?.source === "analysis"
              ? "分析前账户刷新已完成"
              : "上次手动刷新已完成"
          }}
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
      v-if="desktop && data.live_readonly_sync?.status === 'unavailable'"
      class="notice warn"
      role="status"
    >
      暂未读取到持仓。请在京东金融登录并打开对应的积存金页面。
    </div>
    <div
      v-else-if="needsLogin && !desktop"
      class="notice warn"
      role="status"
      style="margin-top: 16px"
    >
      <strong>需要重新登录京东</strong>
      <div class="sub">
        京东会话已失效或尚未建立。需要登录时再开启ECS临时远程浏览器，由你完成登录；登录后自动恢复已启用账户的采集。本看板不接收京东密码或验证码。
      </div>
    </div></Card
  >
</template>
