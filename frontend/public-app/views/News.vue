<script setup>
import { ref, computed } from "vue";
import { useFeed } from "../useFeed.js";
import { time } from "../state.js";
const { data, error, loading } = useFeed("/api/public/market?view=news");
const important = ref(true),
  limit = ref(12);
const items = computed(() =>
  (data.value?.items || []).filter((r) => !important.value || r.important),
);
</script>
<template>
  <div class="view-stack" data-public-view="news" :data-ready="data !== null">
    <div v-if="error" class="notice warn">
      {{
        data ? "快讯更新失败，保留上次内容。" : "暂时无法读取快讯，请稍后刷新。"
      }}
    </div>
    <section class="card">
      <div class="section-heading">
        <div>
          <h2>黄金相关快讯</h2>
        </div>
        <div class="segmented" aria-label="快讯筛选">
          <button
            :aria-pressed="important"
            @click="
              important = true;
              limit = 12;
            "
          >
            重要</button
          ><button
            :aria-pressed="!important"
            @click="
              important = false;
              limit = 12;
            "
          >
            全部
          </button>
        </div>
      </div>
      <p class="muted small">
        近24小时收录 · {{ data?.source || "金十数据" }} · 最新消息
        {{ time(data?.latest_at) }}
      </p>
      <ol v-if="items.length" class="public-news-list">
        <li v-for="item in items.slice(0, limit)" :key="item.id">
          <div class="news-time">
            {{ time(item.at) }}
            <span v-if="item.important" class="status-pill">重要</span>
          </div>
          <p>{{ item.text }}</p>
        </li>
      </ol>
      <div v-else class="empty">
        {{ loading ? "正在读取快讯…" : "当前筛选下暂无快讯" }}
      </div>
      <button
        v-if="items.length > limit"
        class="more-button"
        @click="limit += 12"
      >
        显示更多快讯
      </button>
    </section>
  </div>
</template>
