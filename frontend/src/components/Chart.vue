<script setup>
import { computed, ref, onMounted, onBeforeUnmount, watch } from "vue";
import { num, pct, fmtTs } from "../format";
const props = defineProps({
  series: { type: Array, default: () => [] },
  percent: Boolean,
  emptyText: { type: String, default: "数据积累中" },
  references: { type: Array, default: () => [] },
  unit: { type: String, default: "" },
});
const hidden = ref(new Set()),
  selected = ref(null),
  pinned = ref(false),
  container = ref(null),
  width = ref(760);
let observer;
onMounted(() => {
  const resize = () => {
    const measured = container.value?.clientWidth;
    if (measured > 0) width.value = measured;
  };
  resize();
  observer = new ResizeObserver(resize);
  observer.observe(container.value);
});
onBeforeUnmount(() => observer?.disconnect());
watch(
  () => props.series,
  () => {
    selected.value = null;
    pinned.value = false;
  },
);
const visible = computed(() =>
  props.series
    .filter((s) => !hidden.value.has(s.key))
    .map((s, i) => ({
      ...s,
      color: s.color || `var(--series-${i + 1})`,
      points: (s.points || [])
        .filter(
          (p) =>
            p[1] != null &&
            p[1] !== "" &&
            typeof p[1] !== "boolean" &&
            Number.isFinite(Number(p[1])) &&
            p[0] &&
            Number.isFinite(+new Date(p[0])),
        )
        .sort((a, b) => +new Date(a[0]) - +new Date(b[0])),
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
const left = computed(() =>
  Math.max(58, Math.max(...ticks.value.map((v) => label(v).length)) * 7 + 12),
);
const right = computed(() => width.value - 16);
const X = (t) =>
  left.value +
  ((+new Date(t) - domain.value.start) /
    (domain.value.end - domain.value.start || 1)) *
    (right.value - left.value);
const Y = (v) =>
  190 -
  ((v - domain.value.lo) / (domain.value.hi - domain.value.lo || 1)) * 165;
const label = (v) => (props.percent ? pct(v, 2) : num(v, 2));
const valueLabel = (v) =>
  label(v) + (!props.percent && props.unit ? " " + props.unit : "");
const dateOnly = (v) => typeof v === "string" && /^\d{4}-\d{2}-\d{2}$/.test(v);
const pointDate = (v) => (dateOnly(v) ? v.slice(5) : fmtTs(v));
const dailyDates = computed(() =>
  visible.value.every((s) => s.points.every((p) => dateOnly(p[0]))),
);
const axisDate = (v) =>
  dailyDates.value
    ? new Date(v).toISOString().slice(5, 10)
    : fmtTs(new Date(v).toISOString());
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
    x = ((event.clientX - rect.left) / rect.width) * width.value,
    t =
      domain.value.start +
      ((Math.max(left.value, Math.min(right.value, x)) - left.value) /
        (right.value - left.value)) *
        (domain.value.end - domain.value.start);
  if (event.pointerType === "touch") pinned.value = true;
  else pinned.value = false;
  selected.value = t;
}
function leave() {
  if (!pinned.value) selected.value = null;
}
const rows = computed(() =>
  selected.value != null || visible.value.length === 1
    ? visible.value.map((s) => ({
        key: s.key,
        name: s.name,
        color: s.color,
        p:
          selected.value == null
            ? s.points[s.points.length - 1]
            : s.points.reduce((a, p) =>
                Math.abs(+new Date(p[0]) - selected.value) <
                Math.abs(+new Date(a[0]) - selected.value)
                  ? p
                  : a,
              ),
      }))
    : [],
);
const cursor = computed(() =>
  rows.value.length === 1 ? X(rows.value[0].p[0]) : X(selected.value),
);
const inlineRow = computed(() =>
  !props.percent && rows.value.length === 1 ? rows.value[0] : null,
);
const textWidth = (text) =>
  Array.from(text).reduce((n, ch) => n + (ch.charCodeAt(0) > 255 ? 12 : 7), 0);
const tooltip = computed(() => {
  if (!inlineRow.value) return null;
  const p = inlineRow.value.p,
    w = Math.min(
      right.value - left.value,
      Math.max(96, textWidth(valueLabel(p[1])) + 16),
    );
  const x = Math.max(left.value, Math.min(right.value - w, X(p[0]) - w / 2));
  const y = Math.max(
    20,
    Math.min(148, Y(p[1]) - 50 < 20 ? Y(p[1]) + 8 : Y(p[1]) - 50),
  );
  return { x, y, w, h: 42 };
});
const pointLabels = computed(() => {
  if (!inlineRow.value) return [];
  const s = visible.value[0],
    ps = s.points,
    limit = Math.max(2, Math.floor((right.value - left.value) / 72));
  const low = ps.reduce((a, p, i) => (+p[1] < +ps[a][1] ? i : a), 0);
  const high = ps.reduce((a, p, i) => (+p[1] > +ps[a][1] ? i : a), 0);
  const candidates = [
    ...new Set([ps.length - 1, 0, low, high, ...ps.map((_, i) => i)]),
  ];
  const boxes = [tooltip.value],
    result = [];
  for (const i of candidates) {
    const p = ps[i];
    if (p === inlineRow.value.p) continue;
    const value = label(p[1]),
      w = textWidth(value) + 10;
    const x = Math.max(left.value, Math.min(right.value - w, X(p[0]) - w / 2));
    for (const y of [Y(p[1]) - 22, Y(p[1]) + 8]) {
      const box = { x, y, w, h: 17 };
      if (
        y < 20 ||
        y + 17 > 190 ||
        boxes.some(
          (b) =>
            x < b.x + b.w + 5 &&
            x + w + 5 > b.x &&
            y < b.y + b.h + 4 &&
            y + 21 > b.y,
        )
      )
        continue;
      boxes.push(box);
      result.push({ ...box, p, value, color: s.color });
      break;
    }
    if (result.length >= limit) break;
  }
  return result;
});
</script>
<template>
  <div ref="container" class="native-chart">
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
    <template v-else>
      <div class="chart-plot">
        <svg
          :viewBox="`0 0 ${width} 230`"
          role="img"
          aria-label="收益或价格走势"
          @pointermove="move"
          @pointerdown="move"
          @pointerleave="leave"
        >
          <g v-for="v in ticks" :key="v">
            <line
              :x1="left"
              :x2="right"
              :y1="Y(v)"
              :y2="Y(v)"
              stroke="var(--grid)"
            />
            <text
              class="chart-y-tick"
              :x="left - 8"
              :y="Y(v) + 3"
              text-anchor="end"
            >
              {{ label(v) }}
            </text>
          </g>
          <g
            v-for="r in references.filter(
              (r) => r.value >= domain.lo && r.value <= domain.hi,
            )"
            :key="r.label"
          >
            <line
              :x1="left"
              :x2="right"
              :y1="Y(r.value)"
              :y2="Y(r.value)"
              stroke="var(--text-3)"
              stroke-dasharray="5 5"
            />
            <text :x="right" :y="Y(r.value) - 4" text-anchor="end">
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
            :x="left + (i * (right - left)) / 2"
            y="220"
            :text-anchor="i === 0 ? 'start' : i === 2 ? 'end' : 'middle'"
          >
            {{ axisDate(t) }}
          </text>
          <line
            v-if="selected != null"
            :x1="cursor"
            :x2="cursor"
            y1="20"
            y2="190"
            stroke="var(--text-3)"
            stroke-dasharray="3 3"
          />
          <circle
            v-for="r in rows"
            :key="r.key"
            :cx="X(r.p[0])"
            :cy="Y(r.p[1])"
            r="3.5"
            :fill="r.color"
            stroke="var(--surface)"
            stroke-width="1.5"
          />
          <g v-for="(p, i) in pointLabels" :key="i" class="chart-point-value">
            <circle :cx="X(p.p[0])" :cy="Y(p.p[1])" r="2.5" :fill="p.color" />
            <rect
              :x="p.x"
              :y="p.y"
              :width="p.w"
              :height="p.h"
              rx="3"
              fill="var(--surface)"
              fill-opacity="0.94"
            />
            <text
              :x="p.x + p.w / 2"
              :y="p.y + 12"
              text-anchor="middle"
              :style="{ fill: p.color, fontWeight: 600 }"
            >
              {{ p.value }}
            </text>
          </g>
          <line
            v-if="tooltip"
            :x1="X(inlineRow.p[0])"
            :y1="Y(inlineRow.p[1])"
            :x2="
              Math.max(
                tooltip.x + 4,
                Math.min(tooltip.x + tooltip.w - 4, X(inlineRow.p[0])),
              )
            "
            :y2="
              tooltip.y < Y(inlineRow.p[1]) ? tooltip.y + tooltip.h : tooltip.y
            "
            :stroke="inlineRow.color"
            stroke-opacity="0.5"
          />
        </svg>
        <div
          v-if="tooltip"
          class="chart-inline-tooltip"
          role="status"
          :aria-label="selected == null ? '最新数据' : '选中数据'"
          :style="{
            left: tooltip.x + 'px',
            top: tooltip.y + 'px',
            width: tooltip.w + 'px',
            borderColor: inlineRow.color,
          }"
        >
          <span>{{ pointDate(inlineRow.p[0]) }}</span>
          <strong>{{ valueLabel(inlineRow.p[1]) }}</strong>
        </div>
      </div>
      <div
        v-if="rows.length && !inlineRow"
        class="chart-values"
        role="status"
        :aria-label="selected == null ? '最新数据' : '选中数据'"
      >
        <div v-for="r in rows" :key="r.key" class="chart-value-row">
          <span
            ><span class="swatch" :style="{ background: r.color }"></span
            >{{ r.name }} · {{ pointDate(r.p[0]) }}</span
          >
          <strong>{{ valueLabel(r.p[1]) }}</strong>
        </div>
      </div>
      <div v-else-if="!inlineRow" class="sub chart-hint">
        移动鼠标或轻点折线，查看对应日期和数值。
      </div></template
    >
  </div>
</template>
