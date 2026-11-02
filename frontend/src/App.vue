<script setup>
import {
  computed,
  defineAsyncComponent,
  onMounted,
  onUnmounted,
  watch,
} from "vue";
import {
  tabs,
  currentPage,
  accountView,
  marketView,
  header,
  activeEntry,
  loadPage,
  navigate,
  theme,
  setTheme,
} from "./composables/dashboard";
import Icon from "./components/Icon.vue";
import LoadingPage from "./components/LoadingPage.vue";
import ChunkError from "./components/ChunkError.vue";
const lazy = (loader) =>
  defineAsyncComponent({
    loader,
    loadingComponent: LoadingPage,
    errorComponent: ChunkError,
    delay: 100,
    timeout: 15000,
  });
import { fmtTime } from "./format";
import { clock } from "./clock";
const pages = {
  overview: lazy(() => import("./pages/OverviewPage.vue")),
  strategies: lazy(() => import("./pages/AccountsPage.vue")),
  compare: lazy(() => import("./pages/ComparePage.vue")),
  ai: lazy(() => import("./pages/AIPage.vue")),
  market: lazy(() => import("./pages/MarketPage.vue")),
  records: lazy(() => import("./pages/RecordsPage.vue")),
  notifications: lazy(() => import("./pages/NotificationsPage.vue")),
};
const entry = activeEntry;
const darkMedia = matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  const mode = theme.value;
  document.documentElement.dataset.theme =
    mode === "auto" ? (darkMedia.matches ? "dark" : "light") : mode;
  document.querySelector('meta[name="theme-color"]').content =
    document.documentElement.dataset.theme === "dark" ? "#101010" : "#f6f5f2";
}
watch(theme, applyTheme, { immediate: true });
watch([currentPage, accountView, marketView], () => loadPage(), {
  immediate: true,
});
const updated = computed(() =>
  entry.value.error
    ? entry.value.error
    : entry.value.loading
      ? "刷新中…"
      : header.value?.snapshot?.ts
        ? "行情 " + fmtTime(header.value.snapshot.ts)
        : "点击刷新",
);
let timer, clockTimer;
function visible() {
  if (!document.hidden) loadPage(currentPage.value, { force: true });
}
onMounted(() => {
  timer = setInterval(visible, 60000);
  clockTimer = setInterval(() => {
    if (!document.hidden) clock.value = Date.now();
  }, 1000);
  document.addEventListener("visibilitychange", visible);
  darkMedia.addEventListener("change", applyTheme);
});
onUnmounted(() => {
  clearInterval(timer);
  clearInterval(clockTimer);
  document.removeEventListener("visibilitychange", visible);
  darkMedia.removeEventListener("change", applyTheme);
});
</script>
<template>
  <header class="app-header">
    <div class="header-inner">
      <div class="logo"><i class="logo-mark"></i><span>积存金看板</span></div>
      <nav class="tabs-top" role="tablist" aria-label="页面">
        <button
          v-for="[key, label] in tabs"
          :key="key"
          class="tab"
          role="tab"
          :aria-selected="currentPage === key"
          :aria-controls="'panel-' + key"
          @click="navigate(key)"
        >
          <Icon :name="key" />{{ label }}
        </button>
      </nav>
      <div class="header-meta">
        <span class="chip" id="market-chip"
          ><span
            class="dot"
            :style="{
              background: header?.snapshot?.market_open
                ? 'var(--good)'
                : 'var(--text-3)',
            }"
          ></span
          >{{
            header
              ? header.snapshot?.market_open
                ? "交易中"
                : "休市"
              : "加载中"
          }}</span
        ><button
          class="link-btn updated app-status"
          id="updated"
          type="button"
          aria-label="刷新数据"
          @click="loadPage(currentPage, { force: true })"
        >
          {{ updated }}</button
        ><button
          class="icon-btn"
          type="button"
          :aria-label="`主题：${theme}，点击切换`"
          @click="
            setTheme({ auto: 'light', light: 'dark', dark: 'auto' }[theme])
          "
        >
          {{ { auto: "◐", light: "☀", dark: "☾" }[theme] }}
        </button>
      </div>
    </div>
  </header>
  <main>
    <div v-if="entry.error" class="notice warn page-error" role="status">
      {{ entry.error }}<span v-if="entry.data"> · 暂时保留上次数据</span
      ><button class="link-btn" @click="loadPage(currentPage, { force: true })">
        重新获取 →
      </button>
    </div>
    <KeepAlive
      ><component
        v-if="entry.data || currentPage === 'market'"
        :is="pages[currentPage]"
        :key="currentPage"
        :data="entry.data || {}"
    /></KeepAlive>
    <div
      v-if="!entry.data && currentPage !== 'market'"
      class="card page-loading"
      aria-busy="true"
    >
      {{
        entry.error
          ? "暂未取得数据，请重试"
          : "正在加载" +
            (tabs.find((t) => t[0] === currentPage)?.[1] || "页面") +
            "…"
      }}
    </div>
    <div class="foot">模拟盘为虚拟交易 · 真实建议需自行确认 · 每分钟更新</div>
  </main>
  <nav class="tabbar" role="tablist" aria-label="页面">
    <button
      v-for="[key, label] in tabs"
      :key="key"
      class="tab"
      role="tab"
      :aria-selected="currentPage === key"
      @click="navigate(key)"
    >
      <Icon :name="key" /><span>{{ label }}</span>
    </button>
  </nav>
</template>
