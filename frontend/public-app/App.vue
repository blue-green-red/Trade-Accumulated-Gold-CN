<script setup>
import {
  ref,
  computed,
  defineAsyncComponent,
  onMounted,
  onUnmounted,
  watch,
  nextTick,
} from "vue";
import { analyst, models, names, clock, loadReport } from "./state.js";
const views = {
  market: defineAsyncComponent(() => import("./views/Market.vue")),
  "market/news": defineAsyncComponent(() => import("./views/News.vue")),
  "market/calendar": defineAsyncComponent(() => import("./views/Calendar.vue")),
  "market/indicators": defineAsyncComponent(
    () => import("./views/Indicators.vue"),
  ),
  analysis: defineAsyncComponent(() => import("./views/Analysis.vue")),
  "analysis/history": defineAsyncComponent(() => import("./views/History.vue")),
  recommendations: defineAsyncComponent(
    () => import("./views/Recommendations.vue"),
  ),
};
const choices = [
  ["market", "行情", "黄金行情"],
  ["analysis", "AI分析", "AI市场分析"],
  ["recommendations", "仓位推荐", "仓位操作参考"],
];
const route = () =>
  Object.hasOwn(views, location.hash.slice(1))
    ? location.hash.slice(1)
    : "market";
const page = ref(route());
const parent = computed(() => page.value.split("/")[0]);
const subpages = {
  market: [
    ["market", "金价"],
    ["market/news", "快讯"],
    ["market/calendar", "日历"],
    ["market/indicators", "市场指标"],
  ],
  analysis: [
    ["analysis", "最新观点"],
    ["analysis/history", "历史观点"],
  ],
};
const description = computed(
  () => choices.find(([key]) => key === parent.value)[2],
);
const lastScroll = {};
const lastRoute = {};
function syncRoute() {
  lastScroll[page.value] = window.scrollY;
  lastRoute[parent.value] = page.value;
  page.value = route();
}
function navigate(key) {
  const target = lastRoute[key] || key;
  if (target !== page.value) location.hash = target;
}
function refresh() {
  window.dispatchEvent(new CustomEvent("public-refresh"));
}
watch(page, async (key) => {
  await nextTick();
  requestAnimationFrame(() => scrollTo(0, lastScroll[key] || 0));
});
watch(analyst, () => {
  if (page.value === "analysis" || page.value === "recommendations")
    loadReport();
});
let timer;
onMounted(() => {
  window.addEventListener("hashchange", syncRoute);
  timer = setInterval(() => {
    clock.value = Date.now();
  }, 1000);
});
onUnmounted(() => {
  window.removeEventListener("hashchange", syncRoute);
  clearInterval(timer);
});
</script>
<template>
  <header class="topbar">
    <div class="top-inner">
      <a class="brand" href="#market" aria-label="金析AI首页"
        ><span class="brand-mark">金</span><span>金析AI</span></a
      >
      <span class="edition">公众版</span>
      <button
        class="refresh"
        aria-label="刷新当前页面"
        title="读取已保存的最新数据"
        @click="refresh"
      >
        ↻ <span>刷新</span>
      </button>
    </div>
  </header>
  <main class="shell">
    <div class="page-heading">
      <div>
        <h1>{{ description }}</h1>
        <p class="page-subtitle">
          {{
            parent === "market"
              ? "价格、资讯与市场背景"
              : parent === "analysis"
                ? "市场判断、关键因素与风险"
                : "同一轮观点下的11档仓位情景"
          }}
        </p>
      </div>
      <select
        v-if="parent !== 'market' && models.length > 1"
        v-model="analyst"
        aria-label="选择分析视角"
      >
        <option v-for="model in models" :key="model" :value="model">
          {{ names[model] }}
        </option>
      </select>
      <span
        v-else-if="parent !== 'market' && models.length"
        class="model-badge"
        >{{ names[analyst] }}</span
      >
    </div>
    <nav v-if="subpages[parent]" class="public-subnav" aria-label="页面分类">
      <a
        v-for="[key, label] in subpages[parent]"
        :key="key"
        :href="'#' + key"
        :aria-current="page === key ? 'page' : undefined"
        >{{ label }}</a
      >
    </nav>
    <KeepAlive :max="8"><component :is="views[page]" :key="page" /></KeepAlive>
    <footer class="footnote">
      数据及AI观点仅供参考 · 高风险波段 · 不保证收益<br />报价与观点均标明时间，请结合当前市场判断。
    </footer>
  </main>
  <nav class="bottom-nav" aria-label="公众页面导航">
    <div>
      <button
        v-for="([key, label], i) in choices"
        :key="key"
        :aria-current="parent === key ? 'page' : undefined"
        @click="navigate(key)"
      >
        <svg v-if="i === 0" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 18h18M4 14l5-5 4 3 7-8" />
        </svg>
        <svg v-else-if="i === 1" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 3l2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z" />
        </svg>
        <svg v-else viewBox="0 0 24 24" aria-hidden="true">
          <path d="M4 20V12h4v8M10 20V7h4v13M16 20V3h4v17" />
        </svg>
        {{ label }}
      </button>
    </div>
  </nav>
</template>
