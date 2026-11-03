<script setup>
import Card from "../components/Card.vue";
import {
  navigate,
  selectView,
  theme,
  setTheme,
} from "../composables/dashboard";
defineProps({ data: Object });
</script>
<template>
  <section id="panel-me" class="page-stack" data-page-ready="true">
    <Card title="我的"
      ><div class="menu-list">
        <button @click="selectView('notifications', 'settings')">
          <span
            >微信通知<span class="sub"
              >{{
                data.preferences?.notification_configured
                  ? "已配置 " + data.preferences.channel
                  : "尚未配置推送渠道"
              }}
              · 选择订阅</span
            ></span
          ><span>›</span>
        </button>
        <button @click="selectView('notifications', 'history')">
          <span>发送记录<span class="sub">实际消息、重试与受理状态</span></span
          ><span>›</span>
        </button>
        <button @click="navigate('diagnostics')">
          <span
            >系统状态与接口诊断<span class="sub"
              >京东参考额度、缓存和请求状态</span
            ></span
          ><span>›</span>
        </button>
        <button @click="selectView('ai', 'control')">
          <span>模型控制<span class="sub">开关、手动分析与调度规则</span></span
          ><span>›</span>
        </button>
        <button @click="navigate('records')">
          <span>操作记录<span class="sub">模拟成交、决策与分析历史</span></span
          ><span>›</span>
        </button>
      </div></Card
    >
    <Card title="显示偏好"
      ><div class="chips">
        <button
          v-for="[key, label] in [
            ['auto', '跟随系统'],
            ['light', '浅色'],
            ['dark', '深色'],
          ]"
          :key="key"
          class="filter-btn"
          :aria-pressed="theme === key"
          @click="setTheme(key)"
        >
          {{ label }}
        </button>
      </div></Card
    >
  </section>
</template>
