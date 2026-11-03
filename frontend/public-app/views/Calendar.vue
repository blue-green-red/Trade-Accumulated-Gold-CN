<script setup>
import { computed, ref } from "vue";
import { useFeed } from "../useFeed.js";
import { clock, time } from "../state.js";
import { eventName } from "../events.js";
const { data, error, loading } = useFeed("/api/public/market?view=calendar");
const scope = ref("today"),
  filter = ref("upcoming");
const dayFormatter = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Hong_Kong",
});
const minute = computed(() => Math.floor(clock.value / 60000) * 60000);
const date = (v) => dayFormatter.format(new Date(v));
const events = computed(() =>
  (data.value?.items || [])
    .filter((e) => scope.value === "week" || date(e.at) === date(minute.value))
    .filter(
      (e) =>
        filter.value === "all" ||
        (filter.value === "released" ? e.released : !e.released),
    )
    .sort((a, b) =>
      filter.value === "released"
        ? Date.parse(b.at) - Date.parse(a.at)
        : Date.parse(a.at) - Date.parse(b.at),
    ),
);
const next = computed(
  () =>
    (data.value?.items || [])
      .filter((e) => Date.parse(e.at) > clock.value)
      .sort((a, b) => Date.parse(a.at) - Date.parse(b.at))[0],
);
</script>
<template>
  <div
    class="view-stack"
    data-public-view="calendar"
    :data-ready="data !== null"
  >
    <div v-if="error || (data && data.status !== 'fresh')" class="notice warn">
      日历更新待确认，以下为上次保存的日程。
    </div>
    <section v-if="next" class="card event-highlight">
      <span class="eyebrow">NEXT EVENT</span>
      <h2>下一项重要事件</h2>
      <p>
        <strong>{{ time(next.at) }}</strong> · {{ eventName(next.title) }}
      </p>
      <div class="muted small">实际值公布后，可结合预期与前值判断变化。</div>
    </section>
    <section class="card">
      <div class="section-heading">
        <h2>经济日历</h2>
        <div class="segmented" aria-label="日历范围">
          <button :aria-pressed="scope === 'today'" @click="scope = 'today'">
            今天</button
          ><button :aria-pressed="scope === 'week'" @click="scope = 'week'">
            7天范围
          </button>
        </div>
      </div>
      <div class="calendar-filters">
        <button
          v-for="[key, label] in [
            ['upcoming', '待公布'],
            ['released', '已公布'],
            ['all', '全部'],
          ]"
          :key="key"
          :aria-pressed="filter === key"
          @click="filter = key"
        >
          {{ label }}
        </button>
      </div>
      <div v-if="!events.length" class="empty">
        {{ loading ? "正在读取日历…" : "当前筛选下暂无事件" }}
      </div>
      <ul v-else class="calendar-list">
        <li v-for="e in events" :key="e.at + e.title">
          <div class="news-time">
            {{ time(e.at) }} · {{ e.country === "US" ? "美国" : "中国" }}
            <span v-if="e.tier === 1" class="status-pill">重大</span>
          </div>
          <h3>{{ eventName(e.title) }}</h3>
          <div class="event-values">
            <span
              >实际
              <strong>{{
                e.released ? (e.actual ?? "—") : "待公布"
              }}</strong></span
            ><span>预期 {{ e.forecast ?? "—" }}</span
            ><span>前值 {{ e.previous ?? "—" }}</span>
          </div>
          <div
            v-if="!e.released && Date.parse(e.at) <= clock"
            class="muted small"
          >
            已到公布时间，等待数据更新。
          </div>
        </li>
      </ul>
      <p class="muted small">
        近24小时及未来7天 · 香港/北京时间 · {{ data?.source }} · 更新
        {{ time(data?.fetched_at) }}
      </p>
    </section>
  </div>
</template>
