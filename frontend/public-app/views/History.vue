<script setup>
import { ref, watch } from "vue";
import { analyst, models, syncModels, time } from "../state.js";
import { feeds } from "../cache.js";
import { usePage } from "../usePage.js";
import Assessment from "../components/Assessment.vue";
import KeyLevels from "../components/KeyLevels.vue";
const rows = ref([]),
  cursor = ref(null),
  days = ref(14),
  error = ref(false),
  loading = ref(false),
  ready = ref(false),
  hasNew = ref(false);
const detail = ref(null),
  openId = ref(null),
  detailError = ref(false);
let version = 0,
  detailVersion = 0;
const snapshots = new Map();
const key = () => analyst.value + ":" + days.value;
function available(data, owner) {
  syncModels(data.available_models, owner);
  if (!models.value.includes(owner)) {
    rows.value = [];
    cursor.value = null;
    detail.value = null;
    openId.value = null;
    detailVersion++;
    if (models.value.length) analyst.value = models.value[0];
    return false;
  }
  return true;
}
async function load(force = false) {
  const token = ++version,
    owner = analyst.value,
    span = days.value;
  error.value = false;
  loading.value = true;
  try {
    const data = await feeds.read(
      `/api/public/ai/history?model=${owner}&days=${span}&limit=10`,
      { ttl: 60000, force },
    );
    if (token !== version) return;
    ready.value = true;
    if (!available(data, owner)) return;
    if (!rows.value.length || force) {
      rows.value = data.items;
      cursor.value = data.next_before;
      hasNew.value = false;
      if (force) {
        openId.value = null;
        detail.value = null;
      }
    } else if (data.items[0]?.id !== rows.value[0]?.id) {
      hasNew.value = true;
    } else {
      const fresh = new Map(data.items.map((r) => [r.id, r]));
      rows.value = rows.value.map((r) => fresh.get(r.id) || r);
    }
  } catch {
    if (token === version) error.value = true;
  } finally {
    if (token === version) loading.value = false;
  }
}
async function more() {
  if (!cursor.value || loading.value) return;
  const token = version,
    owner = analyst.value;
  loading.value = true;
  error.value = false;
  try {
    const data = await feeds.read(
      `/api/public/ai/history?model=${owner}&days=${days.value}&limit=10&before=${cursor.value}`,
      { ttl: 60000 },
    );
    if (token !== version || !available(data, owner)) return;
    const known = new Set(rows.value.map((r) => r.id));
    rows.value.push(...data.items.filter((r) => !known.has(r.id)));
    cursor.value = data.next_before;
  } catch {
    if (token === version) error.value = true;
  } finally {
    if (token === version) loading.value = false;
  }
}
async function expand(id) {
  if (openId.value === id) {
    openId.value = null;
    detail.value = null;
    detailVersion++;
    return;
  }
  const token = ++detailVersion,
    owner = analyst.value;
  openId.value = id;
  detail.value = null;
  detailError.value = false;
  try {
    const data = await feeds.read(
      `/api/public/ai/history?model=${owner}&report_id=${id}`,
      { ttl: 60000 },
    );
    if (token === detailVersion && owner === analyst.value) {
      detail.value = data.report;
      detailError.value = !data.report;
    }
  } catch {
    if (token === detailVersion) detailError.value = true;
  }
}
watch([analyst, days], (next, old) => {
  if (old)
    snapshots.set(old[0] + ":" + old[1], {
      rows: rows.value,
      cursor: cursor.value,
      ready: ready.value,
      hasNew: hasNew.value,
    });
  const saved = models.value.includes(analyst.value)
    ? snapshots.get(key())
    : null;
  rows.value = saved?.rows || [];
  cursor.value = saved?.cursor || null;
  ready.value = saved?.ready || false;
  hasNew.value = saved?.hasNew || false;
  openId.value = null;
  detail.value = null;
  detailVersion++;
  load();
});
watch(models, (available) => {
  if (available.includes(analyst.value)) return;
  rows.value = [];
  cursor.value = null;
  detail.value = null;
  openId.value = null;
  detailVersion++;
});
usePage(load, 60000);
</script>
<template>
  <div class="view-stack" data-public-view="history" :data-ready="ready">
    <div class="history-toolbar">
      <span class="muted small">已发布的公众观点 · 按时间倒序</span
      ><select v-model="days" aria-label="历史时间范围">
        <option :value="7">近7天</option>
        <option :value="14">近14天</option>
        <option :value="30">近30天</option>
      </select>
    </div>
    <div class="notice muted small">
      历史观点用于回看当时的判断，不能作为当前操作建议。
    </div>
    <div v-if="error" class="notice warn">
      历史记录暂时无法更新，已有内容仍可查看。
    </div>
    <button v-if="hasNew" class="more-button" @click="load(true)">
      有新观点，点击更新
    </button>
    <div v-if="!rows.length" class="card empty">
      {{ loading ? "正在读取历史观点…" : "这个范围尚无已发布的公众观点" }}
    </div>
    <article v-for="row in rows" :key="row.id" class="card history-card">
      <div class="card-kicker">
        <strong>{{ time(row.created_at) }}</strong
        ><span class="muted small">历史观点</span>
      </div>
      <Assessment :value="row.market_assessment" compact />
      <p>{{ row.outlook }}</p>
      <div class="change-note">
        <span>当轮变化</span>{{ row.change_summary }}
      </div>
      <button class="more-button" @click="expand(row.id)">
        {{ openId === row.id ? "收起该轮分析" : "展开该轮分析" }}
      </button>
      <div v-if="openId === row.id" class="history-detail">
        <div v-if="detailError" class="notice warn">
          该轮详情暂不可用，请收起后重试。
        </div>
        <template v-else-if="detail"
          ><h3>当时的市场依据</h3>
          <ul class="review-list">
            <li v-for="(driver, i) in detail.analysis.drivers" :key="i">
              {{ driver }}
            </li>
          </ul>
          <h3>当时的关注价位</h3>
          <KeyLevels :levels="detail.key_levels" historical />
          <p class="muted small">当时风险：{{ detail.analysis.risk }}</p>
          <div class="muted small">
            行情快照 {{ time(detail.snapshot_at) }} ·
            历史内容，不延长原方案有效期。
          </div></template
        >
        <div v-else class="muted small">正在读取这轮公开详情…</div>
      </div>
    </article>
    <button v-if="cursor" class="more-button" :disabled="loading" @click="more">
      {{ loading ? "正在读取…" : "加载更早的观点" }}
    </button>
  </div>
</template>
