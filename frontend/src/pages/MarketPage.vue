<script setup>
import {
  computed,
  defineAsyncComponent,
  ref,
  watch,
  nextTick,
  onActivated,
} from "vue";
import {
  marketViews,
  marketView,
  marketScroll,
  selectMarketView,
  entryFor,
  currentPage,
} from "../composables/dashboard";
import LoadingPage from "../components/LoadingPage.vue";
import ChunkError from "../components/ChunkError.vue";
defineProps({ data: Object });
const lazy = (loader) =>
  defineAsyncComponent({
    loader,
    loadingComponent: LoadingPage,
    errorComponent: ChunkError,
    delay: 100,
    timeout: 15000,
  });
const views = {
  prices: lazy(() => import("./market/PricePage.vue")),
  news: lazy(() => import("./market/NewsPage.vue")),
  calendar: lazy(() => import("./market/CalendarPage.vue")),
  reference: lazy(() => import("./market/ReferencePage.vue")),
};
const ready = ref(false);
const entry = computed(() => entryFor("market", marketView.value));
const dataReady = computed(() => !!entry.value.data);
watch(marketView, () => (ready.value = false), { flush: "sync" });
function restore() {
  if (currentPage.value === "market")
    window.scrollTo({ top: marketScroll.get(marketView.value) || 0 });
}
function mounted() {
  ready.value = true;
  nextTick(restore);
}
onActivated(() => {
  if (ready.value) nextTick(restore);
});
</script>
<template>
  <section
    id="panel-market"
    class="page-stack"
    :data-page-ready="dataReady && ready ? 'true' : 'false'"
  >
    <nav class="market-subnav" role="tablist" aria-label="行情子页面">
      <button
        v-for="[key, label] in marketViews"
        :key="key"
        role="tab"
        :aria-selected="marketView === key"
        :aria-controls="'market-' + key"
        @click="selectMarketView(key)"
      >
        {{ label }}
      </button>
    </nav>
    <KeepAlive
      ><component
        v-if="dataReady"
        :is="views[marketView]"
        :key="marketView"
        :data="entry.data"
        @ready="mounted"
    /></KeepAlive>
    <div v-if="!dataReady" class="card page-loading" aria-busy="true">
      {{
        entry.loading
          ? "正在加载" +
            marketViews.find(([key]) => key === marketView)?.[1] +
            "…"
          : "暂未取得数据，请点击刷新重试"
      }}
    </div>
  </section>
</template>
