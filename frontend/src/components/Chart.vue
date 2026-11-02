<script setup>
import { computed, ref } from "vue";
import { num, pct, fmtTs } from "../format";
const props = defineProps({
  series: { type: Array, default: () => [] },
  percent: Boolean,
  emptyText: { type: String, default: "数据积累中" },
  references: { type: Array, default: () => [] },
});
const hidden = ref(new Set()),
  hover = ref(null);
const visible = computed(() =>
  props.series
    .filter((s) => !hidden.value.has(s.key))
    .map((s, i) => ({
      ...s,
      color: s.color || `var(--series-${i + 1})`,
      points: (s.points || []).filter(
        (p) => Number.isFinite(Number(p[1])) && p[0],
      ),
    }))
    .filter((s) => s.points.length),
);
const domain = computed(() => {
  const pts = visible.value.flatMap((s) => s.points),
    xs = pts.map((p) => +new Date(p[0])),
    ys = pts.map((p) => Number(p[1]));
  let lo = Math.min(...ys),
    hi = Math.max(...ys);
  if (props.percent) {
    lo = Math.min(0, lo);
    hi = Math.max(0, hi);
  }
  const gap = (hi - lo || Math.abs(lo) * 0.01 || 1) * 0.12;
  return {
    start: Math.min(...xs),
    end: Math.max(...xs),
    lo: lo - gap,
    hi: hi + gap,
  };
});
const X = (t) =>
  40 +
  ((+new Date(t) - domain.value.start) /
    (domain.value.end - domain.value.start || 1)) *
    700;
const Y = (v) =>
  190 -
  ((v - domain.value.lo) / (domain.value.hi - domain.value.lo || 1)) * 165;
const label = (v) => (props.percent ? pct(v, 1) : num(v, 0));
const ticks = computed(() =>
  Array.from(
    { length: 5 },
    (_, i) => domain.value.lo + ((domain.value.hi - domain.value.lo) * i) / 4,
  ),
);
const dates = computed(() =>
  visible.value.length
    ? [
        domain.value.start,
        (domain.value.start + domain.value.end) / 2,
        domain.value.end,
      ]
    : [],
);
const points = (s) =>
  s.points.map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
function toggle(key) {
  const set = new Set(hidden.value);
  set.has(key) ? set.delete(key) : set.add(key);
  hidden.value = set;
}
function move(event) {
  const rect = event.currentTarget.getBoundingClientRect(),
    x = ((event.clientX - rect.left) / rect.width) * 760,
    t =
      domain.value.start +
      ((Math.max(40, Math.min(740, x)) - 40) / 700) *
        (domain.value.end - domain.value.start);
  hover.value = {
    x,
    rows: visible.value.map((s) => ({
      name: s.name,
      color: s.color,
      p: s.points.reduce((a, p) =>
        Math.abs(+new Date(p[0]) - t) < Math.abs(+new Date(a[0]) - t) ? p : a,
      ),
    })),
  };
}
</script>
<template>
  <div class="native-chart">
    <div class="chart-legend">
      <button
        v-for="s in series"
        :key="s.key"
        class="chip-btn"
        type="button"
        :aria-pressed="!hidden.has(s.key)"
        :aria-label="'显示或隐藏 ' + s.name"
        @click="toggle(s.key)"
      >
        <span
          class="swatch"
          :style="{ background: s.color || 'var(--series-1)' }"
        ></span
        >{{ s.name }}
      </button>
    </div>
    <div v-if="!visible.length" class="empty">{{ emptyText }}</div>
    <template v-else
      ><svg
        viewBox="0 0 760 230"
        role="img"
        aria-label="收益或价格走势"
        @pointermove="move"
        @pointerleave="hover = null"
      >
        <g v-for="v in ticks" :key="v">
          <line x1="40" x2="740" :y1="Y(v)" :y2="Y(v)" stroke="var(--grid)" />
          <text x="35" :y="Y(v) + 3" text-anchor="end">{{ label(v) }}</text>
        </g>
        <g
          v-for="r in references.filter(
            (r) => r.value >= domain.lo && r.value <= domain.hi,
          )"
          :key="r.label"
        >
          <line
            x1="40"
            x2="740"
            :y1="Y(r.value)"
            :y2="Y(r.value)"
            stroke="var(--text-3)"
            stroke-dasharray="5 5"
          />
          <text x="740" :y="Y(r.value) - 4" text-anchor="end">
            {{ r.label }}
          </text>
        </g>
        <polyline
          v-for="s in visible"
          :key="s.key"
          :points="points(s)"
          fill="none"
          :stroke="s.color"
          stroke-width="1.8"
        />
        <text
          v-for="(t, i) in dates"
          :key="i"
          :x="40 + i * 350"
          y="220"
          :text-anchor="i === 0 ? 'start' : i === 2 ? 'end' : 'middle'"
        >
          {{ fmtTs(new Date(t).toISOString()) }}
        </text>
        <line
          v-if="hover"
          :x1="hover.x"
          :x2="hover.x"
          y1="20"
          y2="190"
          stroke="var(--text-3)"
          stroke-dasharray="3 3"
        />
      </svg>
      <div v-if="hover" class="sub" role="status">
        {{
          hover.rows
            .map((r) => `${r.name} ${fmtTs(r.p[0])} ${label(r.p[1])}`)
            .join(" · ")
        }}
      </div></template
    >
  </div>
</template>
