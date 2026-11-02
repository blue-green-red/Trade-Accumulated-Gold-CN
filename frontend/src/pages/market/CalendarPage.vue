<script setup>
import { computed, ref, onMounted, onActivated } from "vue";
import Card from "../../components/Card.vue";
import { fmtTs } from "../../format";
const props = defineProps({ data: Object });

import SourceMeta from "../../components/SourceMeta.vue";
import { eventName } from "../../events";
import { clock } from "../../clock";
const calendar = ref("upcoming"),
  scope = ref("today");
const macro = computed(() => props.data.macro || {});
const minute = computed(() => Math.floor(clock.value / 60000) * 60000);
const day = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Hong_Kong",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const available = computed(() =>
  (macro.value.calendar || [])
    .filter(
      (e) =>
        +new Date(e.time_utc) >= minute.value - 86400000 &&
        +new Date(e.time_utc) <= minute.value + 7 * 86400000,
    )
    .map((e) =>
      +new Date(e.time_utc) > minute.value
        ? { ...e, actual: null, released: false }
        : e,
    ),
);
const nextEvent = computed(
  () =>
    available.value
      .filter((e) => e.tier <= 2 && +new Date(e.time_utc) > minute.value)
      .sort((a, b) => +new Date(a.time_utc) - +new Date(b.time_utc))[0],
);
const events = computed(() =>
  available.value
    .filter(
      (e) =>
        scope.value !== "today" ||
        day.format(new Date(e.time_utc)) === day.format(new Date(minute.value)),
    )
    .filter(
      (e) =>
        calendar.value === "all" ||
        (calendar.value === "released"
          ? e.released
          : !e.released && +new Date(e.time_utc) >= minute.value - 1800000),
    )
    .sort((a, b) =>
      calendar.value === "released"
        ? +new Date(b.time_utc) - +new Date(a.time_utc)
        : +new Date(a.time_utc) - +new Date(b.time_utc),
    ),
);
function eventStatus(e) {
  const minutes = (+new Date(e.time_utc) - minute.value) / 60000;
  return e.released
    ? "已公布"
    : minutes < 0
      ? "已到时间 · 待更新"
      : minutes < 60
        ? Math.ceil(minutes) + " 分钟后"
        : "待公布";
}

const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
</script>
<template>
  <div
    id="market-calendar"
    class="page-stack"
    data-market-view="calendar"
    role="tabpanel"
    aria-label="经济日历"
  >
    <Card title="未来七天经济日历"
      ><SourceMeta
        :stamp="macro.calendar_fetched_at || macro.fetched_at"
        :cadence="
          macro.calendar_refresh_seconds === 60
            ? '公布窗口 · 每分钟'
            : '每30分钟'
        "
        :max="macro.calendar_refresh_seconds === 60 ? 180 : 2100"
        :error="macro.refresh_errors?.calendar"
        :missing="!macro.calendar"
      />
      <div
        class="row"
        style="
          justify-content: space-between;
          flex-wrap: wrap;
          margin-top: 10px;
        "
      >
        <div class="market-calendar-filters">
          <div class="seg" role="group" aria-label="日历范围">
            <button :aria-pressed="scope === 'today'" @click="scope = 'today'">
              今天</button
            ><button :aria-pressed="scope === 'week'" @click="scope = 'week'">
              近24小时及未来7天
            </button>
          </div>
          <div class="seg" role="group" aria-label="日历筛选">
            <button
              v-for="[key, label] in [
                ['upcoming', '待公布'],
                ['released', '已公布'],
                ['all', '全部'],
              ]"
              :key="key"
              :aria-pressed="calendar === key"
              @click="calendar = key"
            >
              {{ label }}
            </button>
          </div>
        </div>
        <span class="sub">北京时间 · {{ events.length }} 项</span>
      </div>
      <div v-if="nextEvent" class="notice market-next-event">
        <strong>下一项重要事件</strong> · {{ fmtTs(nextEvent.time_utc) }} ·
        {{ eventName(nextEvent.title) }} · {{ eventStatus(nextEvent) }}
      </div>
      <ul v-if="events.length" class="event-list">
        <li v-for="e in events" :key="e.title + e.time_utc" class="event-row">
          <div class="event-time">
            {{ fmtTs(e.time_utc) }}
            <div class="data-meta">{{ eventStatus(e) }}</div>
          </div>
          <div>
            <div class="event-title">
              <span v-if="e.tier === 1" class="tag ai">重大</span>
              {{ { US: "美国", CN: "中国" }[e.country] || e.country }}
              {{ eventName(e.title) }}
            </div>
            <div class="event-values">
              <span
                >实际 <strong>{{ e.actual ?? "—" }}</strong></span
              ><span>预期 {{ e.forecast ?? "—" }}</span
              ><span>前值 {{ e.previous ?? "—" }}</span>
            </div>
          </div>
        </li>
      </ul>
      <div v-else class="empty">
        {{
          macro.calendar ? "当前筛选下暂无日程" : "日历尚未获取，等待后台更新"
        }}
      </div></Card
    >
  </div>
</template>
