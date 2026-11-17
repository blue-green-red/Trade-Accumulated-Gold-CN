<script setup>
import { num, quantity } from "../format";
defineProps({
  path: Object,
  steps: Array,
  note: String,
  active: Boolean,
  simulation: Boolean,
});
</script>
<template>
  <section v-if="path" class="live-quote" data-position-path>
    <div class="live-section-heading">
      <strong>接下来怎么买、何时考虑卖</strong>
      <span class="sub">{{
        path.state === "boundary_reached"
          ? "已到退出参考价 · 暂停继续买入"
          : !active
            ? "历史路径 · 需重新评估"
            : path.checks?.length
              ? "路径需核对"
              : "本轮人工参考"
      }}</span>
    </div>
    <div class="live-advice-values">
      <div>
        <div class="l">后续满足条件时考虑的仓位</div>
        <div class="v">
          {{
            path.target_position_pct == null
              ? "待确认"
              : num(path.target_position_pct, 1) + "%"
          }}
        </div>
      </div>
      <div>
        <div class="l">暂停加仓并重新考虑卖出的价格</div>
        <div class="v">
          {{
            path.boundary_price_cny == null
              ? "按说明重新分析"
              : num(path.boundary_price_cny) + " 元/克"
          }}
        </div>
      </div>
    </div>
    <p class="sub">
      到上述价格后：{{
        path.exit_scope === "plan"
          ? "停止继续加仓，核对现有持仓和挂单，重新决定是否卖出。"
          : "处理当前这批持仓，其他部分及后续买入需要重新分析。"
      }}
    </p>
    <p
      v-for="(step, index) in steps?.length ? steps : [path.continuation]"
      :key="index"
      class="live-advice-reason"
    >
      {{ step }}
    </p>
    <p class="sub">
      上面的后续目标不是本次买入后的仓位；是否继续买卖，要更新账户后重新分析。
    </p>
    <div v-if="simulation" class="sub">
      当前持仓 {{ quantity(path.input_grams) }}；已设止损覆盖
      {{ quantity(path.stop_coverage_grams) }}，已设止盈覆盖
      {{ quantity(path.target_coverage_grams) }}。
    </div>
    <div v-else-if="path.coverage" class="sub">
      <div>分析时确认持仓 {{ quantity(path.input_grams) }}。</div>
      <div>
        已挂止盈止损单覆盖
        {{ quantity(path.coverage.confirmed_protected_grams) }}；本轮建议新挂
        {{
          quantity(path.coverage.suggested_protected_grams)
        }}，仅针对已确认的持仓。
      </div>
      <div v-if="path.coverage.additional_buy_grams > 0">
        若全部新增买入成交，持仓约
        {{
          quantity(path.coverage.after_all_buys_grams)
        }}；按现有及本轮保护估算，尚未覆盖
        {{
          quantity(path.coverage.uncovered_after_buys_grams)
        }}。新增买入不会自动加入原止盈止损单，成交后需要调整挂单克数。
      </div>
    </div>
    <p class="sub">{{ note || path.execution_note }}</p>
    <p v-if="path.manual_resolution_required" class="live-advice-check">
      须先人工确认旧买单撤改结果，刷新账户后再决定新增买入。
    </p>
    <p
      v-for="check in path.checks || []"
      :key="check"
      class="live-advice-check"
    >
      需核对：{{ check }}
    </p>
  </section>
</template>
