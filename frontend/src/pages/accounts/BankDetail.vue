<script setup>
import { computed, onMounted, onActivated } from "vue";
import { selectView, openBank } from "../../composables/dashboard";
import { lazy } from "../../lazy";
const LiveAccounts = lazy(() => import("../../components/LiveAccounts.vue"));
const props = defineProps({ data: Object });
const emit = defineEmits(["ready"]);
onMounted(() => emit("ready"));
onActivated(() => emit("ready"));
const bank = computed(() => props.data.live_account?.bank);
const wrapped = computed(() => ({
  ...props.data,
  live_readonly: props.data.live_account ? [props.data.live_account] : [],
}));
const views = [
  ["holdings", "持仓"],
  ["advice", "AI建议"],
  ["orders", "条件单"],
  ["records", "记录"],
];
</script>
<template>
  <div class="page-stack">
    <button
      class="link-btn back-link"
      @click="selectView('strategies', 'live')"
    >
      ‹ 返回真实账户
    </button>
    <nav
      v-if="data.live_account"
      class="market-subnav"
      role="tablist"
      :aria-label="(data.live_account?.name || '') + '详情'"
    >
      <button
        v-for="[key, label] in views"
        :key="key"
        role="tab"
        :aria-selected="data.bank_view === key"
        @click="openBank(bank, key)"
      >
        {{ label }}
      </button>
    </nav>
    <div v-if="!data.live_account" class="empty">
      此真实账户已停用或暂不可用，请返回真实账户列表。
    </div>
    <p v-if="data.live_account?.risk_profile" class="sub">
      {{ data.live_account.risk_profile.name || "高风险" }}建议参考 ·
      单次计划风险 {{ data.live_account.risk_profile.trade_risk_pct }}% ·
      回撤参考线 {{ data.live_account.risk_profile.drawdown_limit_pct }}% ·
      需你确认执行
    </p>
    <LiveAccounts
      v-if="data.live_account"
      :data="wrapped"
      :section="data.bank_view"
    />
  </div>
</template>
