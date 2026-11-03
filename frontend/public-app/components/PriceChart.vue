<script setup>
import { computed, ref, watch } from "vue";
import { fmt, time } from "../state.js";
const props = defineProps({ points: Array });
const hover = ref(null);
watch(
  () => props.points,
  () => {
    hover.value = null;
  },
);
const points = computed(() =>
  props.points.filter((p) => p?.[0] && Number.isFinite(p[1])),
);
const domain = computed(() => {
  const values = points.value.map((p) => p[1]);
  const low = Math.min(...values),
    high = Math.max(...values);
  const pad = Math.max((high - low) * 0.16, 0.5);
  return [low - pad, high + pad];
});
const y = (value) =>
  20 + ((domain.value[1] - value) / (domain.value[1] - domain.value[0])) * 190;
const x = (i) => 56 + (i / Math.max(1, points.value.length - 1)) * 480;
const line = computed(() =>
  points.value.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p[1])}`).join(" "),
);
const selected = computed(() =>
  hover.value == null ? Math.max(0, points.value.length - 1) : hover.value,
);
const bubbleX = computed(() => Math.min(498, Math.max(70, x(selected.value))));
function move(e) {
  const r = e.currentTarget.getBoundingClientRect();
  const coordinate = ((e.clientX - r.left) / r.width) * 620;
  hover.value = Math.max(
    0,
    Math.min(
      points.value.length - 1,
      Math.round(((coordinate - 56) / 480) * (points.value.length - 1)),
    ),
  );
}
</script>
<template>
  <div class="chart-wrap">
    <div v-if="!points.length" class="empty chart-empty">
      这个范围暂无有效采样
    </div>
    <template v-else
      ><svg
        class="price-chart"
        viewBox="0 0 620 250"
        role="img"
        aria-label="京东积存金价格走势，数字标注在曲线旁"
        @pointermove="move"
        @pointerleave="hover = null"
        @pointerdown="move"
      >
        <defs>
          <linearGradient id="public-gold-fill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stop-color="var(--series-1)" stop-opacity=".2" />
            <stop offset="100%" stop-color="var(--series-1)" stop-opacity="0" />
          </linearGradient>
        </defs>
        <g v-for="n in 4" :key="n">
          <line
            x1="56"
            x2="536"
            :y1="20 + (n - 1) * 63.33"
            :y2="20 + (n - 1) * 63.33"
            stroke="var(--grid)"
          />
          <text
            x="47"
            :y="24 + (n - 1) * 63.33"
            text-anchor="end"
            fill="var(--text-3)"
            font-size="16"
          >
            {{ fmt(domain[1] - ((domain[1] - domain[0]) * (n - 1)) / 3, 1) }}
          </text>
        </g>
        <path
          :d="line + ' L' + x(points.length - 1) + ',215 L56,215 Z'"
          fill="url(#public-gold-fill)"
        />
        <path
          :d="line"
          fill="none"
          stroke="var(--series-1)"
          stroke-width="2.5"
          stroke-linejoin="round"
          stroke-linecap="round"
        />
        <circle
          :cx="x(selected)"
          :cy="y(points[selected][1])"
          r="4.5"
          fill="var(--series-1)"
          stroke="var(--surface)"
          stroke-width="2"
        />
        <g
          :transform="`translate(${bubbleX},${Math.max(0, y(points[selected][1]) - 41)})`"
        >
          <rect
            x="-52"
            width="104"
            height="32"
            rx="8"
            fill="var(--surface-2)"
          />
          <text
            y="24"
            text-anchor="middle"
            fill="var(--text)"
            font-size="20"
            font-weight="600"
          >
            {{ fmt(points[selected][1]) }}
          </text>
        </g>
        <text x="56" y="244" fill="var(--text-3)" font-size="16">
          {{ time(points[0][0]) }}
        </text>
        <text
          x="536"
          y="244"
          text-anchor="end"
          fill="var(--text-3)"
          font-size="16"
        >
          {{ time(points[points.length - 1][0]) }}
        </text>
      </svg>
      <div class="chart-caption">
        {{ time(points[selected][0]) }}<span>轻触曲线查看</span>
      </div></template
    >
  </div>
</template>
