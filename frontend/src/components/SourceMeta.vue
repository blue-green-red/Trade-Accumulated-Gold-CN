<script setup>
import { computed } from "vue";
import { fmtTs, sourceState } from "../format";
const props = defineProps({
  stamp: String,
  cadence: String,
  max: { type: Number, default: 180 },
  error: [String, Boolean],
  missing: Boolean,
});
const status = computed(() =>
  props.error
    ? props.stamp && !props.missing
      ? "刷新失败 · 保留旧值"
      : "获取失败"
    : props.missing
      ? "暂无数据"
      : sourceState(props.stamp, props.max),
);
</script>
<template>
  <div class="data-meta">
    <span class="data-state" :class="{ warn: status !== '已更新' }">{{
      status
    }}</span
    ><span>{{ fmtTs(stamp) }} · {{ cadence }}</span>
  </div>
</template>
