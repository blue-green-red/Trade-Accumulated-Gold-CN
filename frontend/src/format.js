import { clock } from "./clock.js";
const bj = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Hong_Kong",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
const timeFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Hong_Kong",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});
export const fmtTs = (v) =>
  v && Number.isFinite(new Date(v).getTime())
    ? bj.format(new Date(v)).replaceAll("/", "-")
    : "—";
export const fmtTime = (v) =>
  v && Number.isFinite(new Date(v).getTime())
    ? timeFmt.format(new Date(v))
    : "—";
export const num = (v, n = 2) =>
  v == null || !Number.isFinite(Number(v))
    ? "—"
    : Number(v).toLocaleString("zh-CN", {
        minimumFractionDigits: n,
        maximumFractionDigits: n,
      });
export const signed = (v, n = 2) =>
  v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v), n);
export const pct = (v, n = 2) =>
  v == null
    ? "—"
    : (v > 0 ? "+" : v < 0 ? "−" : "") + num(Math.abs(v) * 100, n) + "%";
export const rate = (v) => (v == null ? "—" : Math.round(v * 100) + "%");
export const money = (v, s = false) =>
  v == null ? "—" : (s ? signed(v) : num(v)) + " 元";
export const quantity = (v) => (v == null ? "未知" : num(v, 4) + " 克");
export const modelNames = { codex: "Codex", claude: "Claude" };
export const actions = {
  buy: "买入",
  sell: "卖出",
  hold: "持有",
  wait: "观望",
  cancel: "撤单",
};
export const bias = { bullish: "偏多", bearish: "偏空", neutral: "中性" };
export const accountLabels = Object.fromEntries(
  Object.entries({
    low: "低风险",
    standard: "标准风险",
    high: "高风险",
    short: "短线",
    medium: "中线",
    long: "长线",
  }).flatMap(([k, v]) =>
    [
      ["full", "满仓"],
      ["empty", "空仓"],
    ].map(([start, label]) => [k + "_" + start, v + " · 初始" + label]),
  ),
);
export const riskMode = (d) => d.trading_mode === "risk_six_account";
export const periodChoices = (d) =>
  riskMode(d)
    ? [
        ["all", "全部风险"],
        ["low", "低风险"],
        ["standard", "标准风险"],
        ["high", "高风险"],
      ]
    : [
        ["all", "全部周期"],
        ["short", "短线"],
        ["medium", "中线"],
        ...(d.trading_mode === "four_account" ? [] : [["long", "长线"]]),
      ];
export const modelChoices = (d) => [
  ["all", "全部模型"],
  ...(d.model_controls || [])
    .filter((c) => c.enabled)
    .map((c) => [c.key, c.name]),
];
export const accountName = (key, d) =>
  d.strategy_names?.[key] ||
  (modelNames[key?.split(":")[0]] || "") +
    " · " +
    (accountLabels[key?.split(":")[1]] || key);
export const pastDeadline = (p) =>
  !!p?.expires_at && new Date(p.expires_at).getTime() <= clock.value;
export const planStatus = (p) =>
  p.status === "pending" && pastDeadline(p) ? "expired" : p.status;
export function statusLabel(p) {
  const s = planStatus(p),
    action = p.decision?.action;
  if (s === "executed" && ["hold", "wait", "cancel"].includes(action))
    return action === "cancel"
      ? "撤单指令已处理（无成交）"
      : pastDeadline(p)
        ? "参考已到期（无成交）"
        : "已记录（无成交）";
  return (
    {
      pending: "待成交",
      executed: "已成交",
      rejected: "已拒绝（未成交）",
      expired: "已到期（未成交）",
      cancelled: "已撤销（未成交）",
    }[s] || s
  );
}
export function validity(p) {
  if (
    ["hold", "wait", "cancel"].includes(p.decision?.action) &&
    p.status === "executed"
  )
    return `决策参考${pastDeadline(p) ? "已到期" : "有效"} · 截止 ${fmtTs(p.expires_at)} · 持仓保护不随参考有效期撤销`;
  return planStatus(p) === "expired"
    ? `委托已到期，不再执行 · 截止 ${fmtTs(p.expires_at)}`
    : `${planStatus(p) === "pending" ? "委托有效至" : "原有效至"} ${fmtTs(p.expires_at)}`;
}
export const executionRange = (d) =>
  d.min_execution_price_cny != null && d.max_execution_price_cny != null
    ? `可接受成交价 ${num(d.min_execution_price_cny)}–${num(d.max_execution_price_cny)} 元/克`
    : "缺少成交价格区间，买卖委托将被拒绝";
export const difference = (v) =>
  v == null
    ? "相对基准待计算"
    : Math.abs(v) < 0.00005
      ? "与基准基本持平"
      : `${v > 0 ? "领先" : "落后"}基准 ${num(Math.abs(v) * 100)} 个百分点`;
export const displayTitle = (v) => (v || "").replaceAll("\u{1F9E0}", "📊");
export const safeUrl = (v) => (/^https?:\/\//i.test(v || "") ? v : "#");
export function totals(rows) {
  const started = rows.filter((a) => a.started),
    sum = (key) =>
      started.length && started.every((a) => Number.isFinite(a[key]))
        ? started.reduce((n, a) => n + a[key], 0)
        : null;
  const budget = sum("budget"),
    value = sum("value");
  return {
    count: rows.length,
    started: started.length,
    budget,
    value,
    cash: sum("cash"),
    pnl: value != null && budget != null ? value - budget : null,
    return: value != null && budget > 0 ? value / budget - 1 : null,
    held: started.filter((a) => a.grams > 1e-9).length,
  };
}
export const sourceState = (stamp, max = 180) =>
  !stamp
    ? "未获取"
    : !Number.isFinite(+new Date(stamp)) ||
        clock.value - new Date(stamp).getTime() > max * 1000 ||
        clock.value - new Date(stamp).getTime() < -60000
      ? "更新延迟"
      : "已更新";

// Rendering must expire a cached quote even between the page's one-minute polls.
export function executionStatus(data) {
  const q = data.execution?.quote || data.execution?.valuation_quote;
  const session = data.snapshot?.jd_session || {};
  const closed =
    session.is_open === false ||
    (session.close_at && new Date(session.close_at).getTime() <= clock.value);
  const ages = q
    ? [q.quote_at, q.fetched_at].map(
        (t) => (clock.value - new Date(t).getTime()) / 1000,
      )
    : [];
  const valid =
    !!data.execution?.ready &&
    q?.source === "jd_latest_price_api" &&
    Number.isFinite(Number(q.price)) &&
    Number(q.price) > 0 &&
    ages.every((age) => Number.isFinite(age) && age >= -30 && age <= 120);
  const cached = valid && !!q.cache_reused;
  return {
    ready: valid && !closed,
    cached: cached && !closed,
    label: closed
      ? "京东休市 · 暂停模拟成交"
      : valid
        ? cached
          ? "沿用缓存 · 仍在120秒有效期内"
          : "有效执行报价"
        : ages.some((age) => Number.isFinite(age) && age > 120)
          ? "报价超过120秒 · 暂停模拟成交"
          : data.execution?.status?.reason === "fetch_failed"
            ? "本轮取价失败 · 暂停模拟成交"
            : "暂无有效报价 · 暂停模拟成交",
    note:
      cached && !closed
        ? "保留原报价时间，到期自动停用"
        : !valid || closed
          ? "历史参考不用于执行"
          : "",
  };
}
