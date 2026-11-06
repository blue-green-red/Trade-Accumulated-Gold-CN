<script setup>
import { num, signed } from "../format";
defineProps({ items: Array });
const roles = {
  stress_test: "压力测试",
  thesis_invalidation: "假设失效",
  risk_limit: "自选风险限制",
  entry_reference: "入场观察",
};
const choices = {
  buy: "加仓",
  hold: "继续持有",
  sell: "减仓或退出",
  reassess: "先复评",
};
</script>
<template>
  <section v-if="items?.length" class="live-quote" data-downside-review>
    <strong>下跌后的方案比较</strong>
    <p class="sub">仅为情景判断；建议挂单和价格复评条件另列。</p>
    <div v-for="(item, i) in items" :key="i" class="scenario">
      <strong>跌到 {{ num(item.price_cny) }} 元/克</strong>
      <div class="sub">
        {{ roles[item.price_role] || "用途待核对" }} · 倾向{{
          choices[item.preferred_action] || "待核对"
        }}
      </div>
      <p class="live-advice-reason">{{ item.basis }}</p>
      <dl>
        <div>
          <dt>加仓</dt>
          <dd>{{ item.buy_case }}</dd>
        </div>
        <div>
          <dt>继续持有</dt>
          <dd>{{ item.hold_case }}</dd>
        </div>
        <div>
          <dt>减仓或退出</dt>
          <dd>{{ item.sell_case }}</dd>
        </div>
      </dl>
      <p v-if="item.account_effect" class="sub">
        维持现仓到该价，账户净值变动约
        {{
          signed(item.account_effect.account_change_pct)
        }}%（一致扣卖费估值，未计跳价）。
      </p>
      <p
        v-for="check in item.checks || []"
        :key="check"
        class="live-advice-check"
      >
        需核对：{{ check }}
      </p>
    </div>
  </section>
</template>
<style scoped>
.scenario + .scenario {
  border-top: 1px solid var(--line);
  margin-top: 12px;
  padding-top: 12px;
}
dl {
  margin: 8px 0;
}
dl > div {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 8px;
  margin: 8px 0;
}
dt {
  font-weight: 600;
}
dd {
  margin: 0;
  overflow-wrap: anywhere;
}
</style>
