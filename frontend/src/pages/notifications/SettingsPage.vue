<script setup>
import { computed, onMounted, onActivated } from "vue";
import { navigate } from "../../composables/dashboard";
import Card from "../../components/Card.vue";
import { fmtTs } from "../../format";
import {
  pending,
  actionErrors,
  toggleNotification,
} from "../../composables/dashboard";
const props = defineProps({ data: Object });
const notices = computed(() => props.data.notifications || {});
const loginWatch = computed(() => notices.value.login_watch || {});
const loginPhases = {
  disabled: "已关闭自动检查",
  healthy: "登录正常",
  checking: "正在核对登录状态",
  unknown: "检查暂未完成，未判定登录失效",
  waiting_for_resources: "等待资源后开启登录入口",
  opening: "正在开启登录入口",
  entry_ready: "登录入口已开启，请登录",
  entry_expired: "入口已关闭，等待下一次检查",
};
const entryReady = computed(
  () =>
    loginWatch.value.phase === "entry_ready" &&
    new Date(loginWatch.value.entry_expires_at).getTime() > Date.now(),
);
const groups = computed(() =>
  [
    {
      title: "京东账户登录维护",
      description:
        "每小时核对登录状态，确认失效后自动开启15分钟登录入口并提醒。同一入口只提醒一次，未登录则下一小时再检查；与模型开关独立。恢复同步后自动关闭本功能开启的浏览器。",
      rows: (notices.value.subscriptions || []).filter(
        (s) => s.key === "jd:login",
      ),
    },
    {
      title: "真实账户建议 · 人工执行",
      description:
        "按模型与银行分别订阅。买卖或价格/数量、挂单撤改实质调整才提醒；建议到期、数据过期或账户变化后，不发送旧建议。",
      rows: (notices.value.subscriptions || []).filter((s) =>
        s.key.includes(":live_"),
      ),
    },
    {
      title: "模型分析摘要",
      description: "每轮成功分析的研究摘要，与真实建议和模拟成交独立。",
      rows: (notices.value.subscriptions || []).filter((s) =>
        s.key.endsWith(":analysis"),
      ),
    },
    {
      title: "模拟盘成交与保护退出",
      description: "每个风险档包含初始满仓和空仓两户，这些是模拟账户内的成交。",
      rows: (notices.value.subscriptions || []).filter(
        (s) =>
          !s.key.includes(":live_") &&
          !s.key.endsWith(":analysis") &&
          s.key !== "jd:login",
      ),
    },
  ].filter((g) => g.rows.length),
);
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div class="page-stack">
    <button class="link-btn back-link" @click="navigate('me')">
      ‹ 返回我的
    </button>
    <Card title="微信操作通知"
      ><div class="sub" role="status">
        {{
          notices.configured
            ? "已配置 " + notices.channel + " · 修改后即时生效"
            : "尚未配置推送密钥，请在服务器配置后重启监控服务。"
        }}
      </div>
      <p class="sub">
        可订阅京东登录失效、真实账户建议、模型分析摘要和模拟盘成交。行情异动与复评事件只在内部调度，不发微信；通知开关与模型开关独立。
      </p>
      <div class="grid-2">
        <section v-for="g in groups" :key="g.title">
          <div class="row">
            <strong class="grow">{{ g.title }}</strong
            ><span class="tag"
              >{{ g.rows.filter((s) => s.enabled).length }}/{{
                g.rows.length
              }}
              已开启</span
            >
          </div>
          <p class="sub">{{ g.description }}</p>
          <div v-if="g.rows.some((s) => s.key === 'jd:login')" class="notice">
            <strong>{{
              loginPhases[loginWatch.phase] || "等待首次检查"
            }}</strong>
            <div v-if="loginWatch.checked_at" class="sub">
              上次检查 {{ fmtTs(loginWatch.checked_at) }}
            </div>
            <div v-if="loginWatch.next_check_at" class="sub">
              下次检查 {{ fmtTs(loginWatch.next_check_at) }}
            </div>
            <template v-if="entryReady">
              <div class="sub">
                入口有效至
                {{ fmtTs(loginWatch.entry_expires_at) }}，使用现有看板登录密码。
              </div>
              <a
                :href="loginWatch.entry_url"
                target="_blank"
                rel="noopener noreferrer"
                >打开京东登录入口</a
              >
            </template>
          </div>
          <div v-for="s in g.rows" :key="s.key" style="margin-bottom: 10px">
            <div class="row">
              <strong class="grow">{{ s.name }}</strong
              ><button
                class="chip-btn"
                role="switch"
                type="button"
                :aria-label="s.name + ' 通知'"
                :aria-checked="!!s.enabled"
                :disabled="pending['notify:' + s.key]"
                @click="toggleNotification(s.key, !s.enabled)"
              >
                {{ s.enabled ? "已开启通知" : "已关闭通知" }}
              </button>
            </div>
            <div
              v-if="actionErrors['notify:' + s.key]"
              class="notice warn"
              role="alert"
            >
              {{ actionErrors["notify:" + s.key] }}
            </div>
          </div>
        </section>
      </div></Card
    >
  </div>
</template>
