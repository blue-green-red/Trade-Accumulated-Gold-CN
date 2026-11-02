<script setup>
import { computed, ref, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
const props = defineProps({ data: Object });

import { watch } from "vue";
const flash = ref("important"),
  limit = ref(8);
watch(flash, () => (limit.value = 8));
const flashes = computed(() =>
  (props.data.jin10 || []).filter((x) => flash.value === "all" || x.important),
);

const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div
    id="market-news"
    class="page-stack"
    data-market-view="news"
    role="tabpanel"
    aria-label="黄金快讯"
  >
    <Card title="黄金相关快讯"
      ><div class="seg" role="group" aria-label="快讯筛选">
        <button :aria-pressed="flash === 'all'" @click="flash = 'all'">
          全部快讯</button
        ><button
          :aria-pressed="flash === 'important'"
          @click="flash = 'important'"
        >
          只看重要
        </button>
      </div>
      <ul v-if="flashes.length" class="list">
        <li v-for="x in flashes.slice(0, limit)" :key="x.id">
          <div class="row">
            <span class="sub">{{ x.time_bj }}</span
            ><span v-if="x.important" class="tag ai">重要</span>
          </div>
          <div>{{ x.text }}</div>
        </li>
      </ul>
      <div v-else class="empty">暂无相关快讯</div>
      <button
        v-if="flashes.length > limit"
        class="link-btn"
        @click="limit += 8"
      >
        显示更多快讯 →
      </button></Card
    >
  </div>
</template>
