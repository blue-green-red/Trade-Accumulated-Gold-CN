<script setup>
import { num, money } from "../format";
defineProps({
  risk: Object,
  profile: Object,
  position: Number,
  historical: Boolean,
});
</script>
<template>
  <section v-if="risk" class="live-quote" data-live-risk>
    <strong>{{ historical ? "本轮分析输入" : "当前账户仓位与风险" }}</strong>
    <div class="live-advice-values">
      <div>
        <div class="l">黄金仓位</div>
        <div class="v">{{ num(position, 1) }}%</div>
      </div>
      <div>
        <div class="l">扣卖费估值</div>
        <div class="v">{{ money(risk.net_assets_cny) }}</div>
      </div>
      <div>
        <div class="l">单次风险参考 {{ profile?.trade_risk_pct ?? "—" }}%</div>
        <div class="v">{{ money(risk.risk_budget_cny) }}</div>
      </div>
      <div>
        <div class="l">金价跌1%的账户影响</div>
        <div class="v">
          {{
            risk.gold_down_1pct_nav_loss_pct == null
              ? "—"
              : "−" + num(risk.gold_down_1pct_nav_loss_pct) + "%"
          }}
        </div>
      </div>
    </div>
    <p class="sub">
      仅本银行黄金＋电子户现金，不含绑定银行卡。卖费为清算估值预留，尚未扣款；风险参考不是必须用满的配额，历史峰值回撤仍未知。
    </p>
  </section>
</template>
