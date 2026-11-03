<script setup>
import { computed, ref, watch, nextTick, onActivated } from "vue";
import {
  entryFor,
  pageView,
  selectView,
  restoreScroll,
} from "../composables/dashboard";
const props = defineProps({
  page: String,
  views: Array,
  components: Object,
  resolve: Function,
  hideTabs: Boolean,
});
const view = computed(() => pageView(props.page));
const entry = computed(() => entryFor(props.page, view.value));
const ready = ref(false);
watch(view, () => (ready.value = false), { flush: "sync" });
const selected = computed(() =>
  view.value === "auto"
    ? entry.value.data?.account_view
    : view.value.split("/")[0],
);
const component = computed(
  () =>
    props.components?.[view.value] ||
    props.resolve?.(view.value, entry.value.data),
);
function mounted() {
  ready.value = true;
  nextTick(() => restoreScroll(props.page, view.value));
}
onActivated(() => {
  if (ready.value) nextTick(() => restoreScroll(props.page, view.value));
});
</script>
<template>
  <section
    :id="'panel-' + page"
    class="page-stack"
    :data-page-ready="entry.data && ready ? 'true' : 'false'"
  >
    <nav
      v-if="views?.length && !hideTabs"
      class="market-subnav"
      role="tablist"
      :aria-label="
        page === 'market'
          ? '行情子页面'
          : page === 'ai'
            ? 'AI子页面'
            : page === 'notifications'
              ? '通知子页面'
              : '账户视图'
      "
    >
      <button
        v-for="[key, label] in views"
        :key="key"
        role="tab"
        :aria-selected="selected === key"
        @click="selectView(page, key)"
      >
        {{ label }}
      </button>
    </nav>
    <KeepAlive :max="24"
      ><component
        v-if="entry.data"
        :is="component"
        :key="view"
        :data="entry.data"
        @ready="mounted"
    /></KeepAlive>
    <div v-if="!entry.data" class="card page-loading" aria-busy="true">
      {{ entry.loading ? "正在加载…" : "暂未取得数据，请点击刷新重试" }}
    </div>
  </section>
</template>
