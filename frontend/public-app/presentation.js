// Display states expire on the local clock; refreshing the page never renews data.
export function freshness(status, at, maxSeconds, now = Date.now()) {
  if (status === "unavailable" || !Number.isFinite(Date.parse(at)))
    return "unavailable";
  const age = now - Date.parse(at);
  if (age < -30000 || age > maxSeconds * 1000) return "stale";
  return status || "unavailable";
}
export const dataLabels = {
  fresh: "已更新",
  partial: "部分数据可用",
  stale: "上次数据 · 待更新",
  unavailable: "暂无数据",
};
export function referenceStatus(value, failed, now = Date.now()) {
  if (!value?.analysis) return "unavailable";
  if (Date.parse(value.expires_at) <= now) return "expired";
  if (failed) return "refresh_failed";
  return value.status === "current" ? "current" : value.reason || "stale";
}
export function referenceDeadline(at, now = Date.now()) {
  return Date.parse(at) <= now ? "参考已到期 · 原截止" : "参考截止";
}
