<script setup>
import { computed, onMounted, onActivated } from "vue";
import Card from "./Card.vue";
import { entryFor, loadPage } from "../composables/dashboard";
import { fmtTs } from "../format";
const entry = entryFor("diagnostics");
const research = computed(() => entry.data?.jd_research || {});
const quota = computed(() => research.value.quota || {});
const parts = computed(() => [
  ["资金", research.value.funds || {}],
  ["挂单", research.value.orders || {}],
]);
function refresh() {
  return loadPage("diagnostics", { force: true });
}
onMounted(() => {
  loadPage("diagnostics");
  document
    .getElementById("system-diagnostics")
    ?.scrollIntoView({ block: "start" });
});
onActivated(() => loadPage("diagnostics"));
const statusLabels = {
  running: "结果待记录",
  fresh: "参考数据可用",
  stale: "来源数据过期",
  cache_stale: "缓存待刷新",
  unavailable: "获取失败",
  not_fetched: "等待分析时获取",
  unconfigured: "尚未配置",
  quota_exhausted: "京东额度耗尽",
  budget_exhausted: "本地预算用完",
  rate_limited: "请求频率受限",
  refreshing: "正在获取",
  no_data: "暂无源数据",
};
const errorLabels = {
  LocalDailyBudget: "本地每日请求预算已用完",
  RequestInterrupted: "上次请求未记录结果，等待重试",
  ProviderDailyQuota: "供应商报告当日额度耗尽",
  ProviderRateLimit: "供应商限制请求频率",
  ProviderUnauthorized: "接口授权失败",
  ProviderRejected: "接口返回失败",
  TimeoutError: "接口请求超时",
  URLError: "网络请求失败",
  ValueError: "返回数据未通过校验",
  JSONDecodeError: "响应格式错误",
  InvalidCachedData: "缓存数据格式异常",
};
const quotaDescriptions = {
  quota_exhausted:
    "京东返回当日额度耗尽，两个参考接口暂停24小时；此时间是本地重试时间，不代表供应商确认的重置时间。",
  budget_exhausted:
    "本地每日请求预算已用完；次日北京时间额度恢复，等待分析触发更新。",
  rate_limited: "京东限制请求频率，短暂暂停两个参考接口，避免连续重试。",
};
</script>
<template>
  <div class="page-stack">
    <div v-if="entry.error" class="notice warn" role="status">
      {{ entry.error }}
    </div>
    <div v-if="!entry.data" class="sub">
      {{ entry.loading ? "正在加载接口状态…" : "尚未取得接口状态" }}
    </div>
    <Card v-else title="京东参考接口状态"
      ><template #action
        ><button class="link-btn" :disabled="entry.loading" @click="refresh">
          刷新状态
        </button></template
      >
      <div v-if="quota.daily_cap != null" class="sub">
        后台参考请求：今日 {{ quota.used_today }}/{{ quota.daily_cap }} 次 ·
        两个模型、两个接口共享预算
      </div>
      <p class="sub">本地计数不包含历史调试或外部调用，不是京东剩余额度。</p>
      <div v-if="quotaDescriptions[quota.status]" class="notice warn">
        {{ quotaDescriptions[quota.status] }}
        <div v-if="quota.retry_at" class="sub">
          可重试时间 {{ fmtTs(quota.retry_at) }}
        </div>
      </div>
      <details v-if="quota.recent_requests?.length" class="more">
        <summary>最近接口请求</summary>
        <ul class="list">
          <li v-for="(r, i) in quota.recent_requests" :key="i">
            {{ fmtTs(r.started_at) }} ·
            {{ r.kind === "funds" ? "资金" : "挂单" }} ·
            {{ statusLabels[r.status] || r.status }}
            <div v-if="r.error_type" class="sub">
              {{ errorLabels[r.error_type] || "接口请求失败" }}
            </div>
          </li>
        </ul>
      </details>
      <div v-for="[name, part] in parts" :key="name" class="live-quote">
        <strong
          >{{ name }} · {{ statusLabels[part.status] || "暂无数据" }}</strong
        >
        <div class="sub">
          源发布 {{ fmtTs(part.published_at) }} · 成功获取
          {{ fmtTs(part.fetched_at) }}
        </div>
        <div v-if="part.attempted_at" class="sub">
          最近尝试 {{ fmtTs(part.attempted_at) }}
        </div>
        <div v-if="part.error_type" class="sub">
          {{ errorLabels[part.error_type] || "接口请求失败" }}
        </div>
        <div v-if="part.retry_at" class="sub">
          暂停请求至 {{ fmtTs(part.retry_at) }}，之后等待正常分析触发重试
        </div>
      </div></Card
    >
  </div>
</template>
