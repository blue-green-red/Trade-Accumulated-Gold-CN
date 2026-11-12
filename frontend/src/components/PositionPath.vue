<script setup>
import { num, quantity } from "../format";
defineProps({ path: Object, active: Boolean, simulation: Boolean });
</script>
<template>
  <section v-if="path" class="live-quote" data-position-path>
    <div class="live-section-heading">
      <strong>建仓与退出路径</strong>
      <span class="sub">{{
        path.state === "boundary_reached"
          ? "边界已触达 · 暂停追加"
          : !active
            ? "历史路径 · 需重新评估"
            : path.checks?.length
              ? "路径需核对"
              : "本轮人工参考"
      }}</span>
    </div>
    <div class="live-advice-values">
      <div>
        <div class="l">目标黄金仓位</div>
        <div class="v">
          {{
            path.target_position_pct == null
              ? "待确认"
              : num(path.target_position_pct, 1) + "%"
          }}
        </div>
      </div>
      <div>
        <div class="l">暂停追加 / 退出边界</div>
        <div class="v">
          {{
            path.boundary_price_cny == null
              ? "按说明复评"
              : num(path.boundary_price_cny) + " 元/克"
          }}
        </div>
      </div>
    </div>
    <p class="sub">
      边界影响：{{
        path.exit_scope === "plan"
          ? "整套建仓结束，重新评估后才能开启新方案。"
          : "当前这批仓位退出；建仓思路是否继续，仍须按新快照重评。"
      }}
    </p>
    <p class="live-advice-reason">{{ path.continuation }}</p>
    <div v-if="simulation" class="sub">
      当前持仓 {{ quantity(path.input_grams) }}；已设止损覆盖
      {{ quantity(path.stop_coverage_grams) }}，已设止盈覆盖
      {{ quantity(path.target_coverage_grams) }}。
    </div>
    <div v-else-if="path.coverage" class="sub">
      <div>分析时确认持仓 {{ quantity(path.input_grams) }}。</div>
      <div>
        已挂组合保护
        {{
          quantity(path.coverage.confirmed_protected_grams)
        }}；本轮新挂保护建议
        {{
          quantity(path.coverage.suggested_protected_grams)
        }}，仅覆盖确认现仓。
      </div>
      <div v-if="path.coverage.additional_buy_grams > 0">
        若全部新增买入成交，持仓约
        {{
          quantity(path.coverage.after_all_buys_grams)
        }}；按现有及本轮保护估算，尚未覆盖
        {{
          quantity(path.coverage.uncovered_after_buys_grams)
        }}。保护不自动扩量，成交后须重评。
      </div>
    </div>
    <p class="sub">{{ path.execution_note }}</p>
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
