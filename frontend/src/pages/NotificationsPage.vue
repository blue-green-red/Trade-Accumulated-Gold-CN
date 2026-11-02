<script setup>
import { computed } from "vue";
import Card from "../components/Card.vue";
import {
  pending,
  actionErrors,
  toggleNotification,
} from "../composables/dashboard";
import { displayTitle, fmtTs } from "../format";
const props = defineProps({ data: Object });
const notices = computed(() => props.data.notifications || {});
const groups = computed(() =>
  [
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
        (s) => !s.key.includes(":live_") && !s.key.endsWith(":analysis"),
      ),
    },
  ].filter((g) => g.rows.length),
);
const category = (key) =>
  key.startsWith("live-advice:")
    ? "真实账户建议"
    : key.startsWith("autonomous:")
      ? "模拟盘操作"
      : key.startsWith("ai:")
        ? "模型摘要"
        : "历史系统事件";
</script>
<template>
  <section id="panel-notifications" class="page-stack" data-page-ready="true">
    <Card title="微信操作通知"
      ><div class="sub" role="status">
        {{
          notices.configured
            ? "已配置 " + notices.channel + " · 修改后即时生效"
            : "尚未配置推送密钥，请在服务器配置后重启监控服务。"
        }}
      </div>
      <p class="sub">
        仅订阅真实账户建议、模型分析摘要和模拟盘成交。行情异动与复评事件只在内部调度，不发微信；通知开关与模型开关独立。
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
    ><Card v-if="notices.outbox?.length" title="发送重试与待核对"
      ><p class="sub">
        明确失败最多尝试3次；超时等受理结果不明的消息保留待核对，避免重复提醒。
      </p>
      <ul class="list">
        <li v-for="r in notices.outbox" :key="r.key">
          <strong>{{ displayTitle(r.title) }}</strong>
          <div class="sub">
            {{
              {
                pending: "待发送",
                retry: "等待重试",
                uncertain: "受理结果待核对",
                cancelled: "已停止发送",
                failed: "重试已结束",
                sending: "正在发送",
              }[r.status] || r.status
            }}
            · 已尝试 {{ r.attempts }} 次<span v-if="r.status === 'retry'">
              · 下次 {{ fmtTs(r.next_attempt_at) }}</span
            >
          </div>
        </li>
      </ul></Card
    ><Card v-if="notices.deliveries?.length" title="微信发送回执"
      ><p class="sub">
        这里显示实际发送的消息标题；服务受理不代表手机已收到或已读。
      </p>
      <ul class="list">
        <li v-for="r in notices.deliveries" :key="r.id">
          <div class="row">
            <strong class="grow">{{ displayTitle(r.title) }}</strong
            ><span class="tag">{{
              {
                accepted: "服务已受理",
                failed: "发送失败",
                uncertain: "受理结果待核对",
                not_sent: "未发送",
              }[r.status] || "状态未知"
            }}</span>
          </div>
          <div class="sub">
            {{ fmtTs(r.ts) }} ·
            {{
              r.category === "real_advice"
                ? "真实账户建议独立通知"
                : "分析/操盘等通知"
            }}
          </div>
          <details v-if="r.provider_id" class="more">
            <summary>查看消息流水号</summary>
            <code style="overflow-wrap: anywhere">{{ r.provider_id }}</code>
          </details>
        </li>
      </ul></Card
    ><Card title="最近通知记录"
      ><p class="sub">
        真实建议单独发送，同轮两家真实建议可合并；事件记录条数不等于微信消息条数。已受理仅表示推送服务接收。
      </p>
      <ul v-if="notices.recent?.length" class="list">
        <li v-for="(r, i) in notices.recent.slice(0, 6)" :key="r.key + ':' + i">
          <div class="row">
            <strong class="grow">{{ displayTitle(r.title) }}</strong
            ><span class="tag">{{
              r.delivered ? "服务已受理" : "仅记录"
            }}</span>
          </div>
          <div class="sub">{{ fmtTs(r.ts) }} · {{ category(r.key) }}</div>
        </li>
      </ul>
      <div v-else class="empty">暂无发送记录</div>
      <details v-if="notices.recent?.length > 6" class="more">
        <summary>较早记录 · {{ notices.recent.length - 6 }} 条</summary>
        <ul class="list">
          <li v-for="(r, i) in notices.recent.slice(6)" :key="r.key + ':' + i">
            <strong>{{ displayTitle(r.title) }}</strong>
            <div class="sub">
              {{ fmtTs(r.ts) }} · {{ r.delivered ? "服务已受理" : "仅记录" }}
            </div>
          </li>
        </ul>
      </details></Card
    >
  </section>
</template>
