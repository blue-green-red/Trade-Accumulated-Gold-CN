<script setup>
import { computed, watch } from "vue";
import Card from "./Card.vue";
import { entryFor, loadPage } from "../composables/dashboard";
import { num, rate } from "../format";
const props = defineProps({ controls: Array });
const entry = entryFor("scorecard"),
  score = computed(() => entry.data?.scorecard);
watch(
  () => props.controls,
  () => loadPage("scorecard", { force: true }),
  { immediate: true },
);
</script>
<template>
  <Card
    title="AI 判断成绩单"
    :subtitle="score ? `${score.runs} 轮 · ${score.views} 次判断` : ''"
    ><div v-if="entry.loading && !score" class="empty">正在读取预测成绩…</div>
    <div v-else-if="entry.error" class="notice warn">
      {{ entry.error
      }}<button
        class="link-btn"
        @click="loadPage('scorecard', { force: true })"
      >
        重试
      </button>
    </div>
    <div v-else-if="!score?.views || !score?.analysts?.length" class="empty">
      暂无 AI 判断记录
    </div>
    <template v-else
      ><div
        v-if="!score.analysts.some((a) => a.horizons.some((h) => h.done))"
        class="notice info"
      >
        已记录 {{ score.views }} 次判断，暂无到期结果。
      </div>
      <div
        v-for="(h0, i) in score.analysts[0].horizons"
        :key="h0.label"
        class="hz"
      >
        <strong>{{ h0.label }}</strong
        ><span class="sub"> · 阈值 ±{{ num(h0.threshold * 100, 1) }}%</span>
        <div v-for="a in score.analysts" :key="a.name">
          <div class="hz-row">
            <span>{{ a.name }}</span>
            <div class="bar">
              <i
                v-if="a.horizons[i].done"
                :style="{ width: a.horizons[i].hit_rate * 100 + '%' }"
              ></i
              ><span class="mid"></span>
            </div>
            <span style="text-align: right"
              ><strong>{{ rate(a.horizons[i].hit_rate) }}</strong>
              <div class="sub">
                已到期 {{ a.horizons[i].done }} 次 · 待到期
                {{ a.horizons[i].pending }}
              </div></span
            >
          </div>
          <div v-if="a.horizons[i].brier_n" class="sub">
            概率准确度 {{ num(a.horizons[i].brier) }}（基准
            {{ num(a.horizons[i].brier_base) }}，越低越准）
          </div>
        </div>
        <div v-if="score.agreement?.[i]?.pairs" class="sub">
          两者一致 {{ score.agreement[i].agree_n }} 轮（命中
          {{ rate(score.agreement[i].agree_hit_rate) }}） · 分歧
          {{ score.agreement[i].disagree_n }} 轮
        </div>
      </div>
      <details class="more">
        <summary>计分规则</summary>
        <p class="sub">
          看多、看空分别以超过涨跌阈值为命中，中性以区间内为命中。新判断从分析完成后首个有效行情起算，5分钟内无有效行情记为缺数据。1日周期在判断后的首个纽约收盘到期，其余为3、20、60日。预测命中率与交易胜率分别统计，操盘结果见操盘对比。样本少时结论不可靠。
        </p>
      </details></template
    ></Card
  >
</template>
