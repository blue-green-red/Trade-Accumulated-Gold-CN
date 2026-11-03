import { ref, computed } from "vue";
import { feeds } from "./cache.js";
import { referenceStatus } from "./presentation.js";
export const clock = ref(Date.now());
export const analyst = ref("primary");
export const models = ref(["primary"]);
export const report = ref(null);
export const reportError = ref(false);
export const reportLoading = ref(false);
let generation = 0;
export function syncModels(available, owner) {
  const next = available || [owner];
  const removed = models.value.filter((model) => !next.includes(model));
  models.value = next;
  for (const model of removed) feeds.forgetModel(model);
  if (removed.includes(analyst.value)) {
    generation++;
    report.value = null;
    reportError.value = false;
    reportLoading.value = false;
  }
}
export async function loadReport(force = false) {
  const token = ++generation;
  const owner = analyst.value;
  const url = `/api/public/ai?model=${owner}`;
  report.value = feeds.peek(url) || null;
  reportLoading.value = true;
  reportError.value = false;
  try {
    const data = await feeds.read(url, { force });
    if (token !== generation || analyst.value !== owner) return;
    syncModels(data.available_models, owner);
    if (models.value.length && !models.value.includes(owner)) {
      analyst.value = models.value[0];
      return loadReport(force);
    }
    report.value = data;
  } catch {
    if (token === generation) reportError.value = true;
  } finally {
    if (token === generation) reportLoading.value = false;
  }
}
export const reportStatus = computed(() =>
  referenceStatus(report.value, reportError.value, clock.value),
);
export const statusText = {
  current: "本轮最新参考",
  unavailable: "等待公开分析",
  expired: "方案已到期，等待新分析",
  market_closed: "当前休市，方案仅供回顾",
  quote_unavailable: "报价待更新，方案待复核",
  price_changed: "京东参考价变化已达阈值，方案需复核",
  review_due: "已触及本轮关注点，需结合当前行情复核",
  stale: "方案待复核",
  refresh_failed: "更新失败，已有方案仅供回顾",
};
export const fmt = (v, digits = 2) =>
  v == null ||
  typeof v === "boolean" ||
  String(v).trim() === "" ||
  !Number.isFinite(Number(v))
    ? "—"
    : Number(v).toLocaleString("zh-CN", {
        minimumFractionDigits: digits,
        maximumFractionDigits: digits,
      });
const timeFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Hong_Kong",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
export const time = (v) =>
  v && Number.isFinite(Date.parse(v)) ? timeFormatter.format(new Date(v)) : "—";
export const names = { primary: "金析AI", secondary: "补充视角" };
export const actions = {
  buy: "增加仓位",
  sell: "降低仓位",
  hold: "维持仓位",
  wait: "继续观望",
};
