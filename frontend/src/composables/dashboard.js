import {
  computed,
  markRaw,
  reactive,
  ref,
  shallowReactive,
  shallowRef,
} from "vue";
export const tabs = [
  ["overview", "总览"],
  ["market", "行情"],
  ["strategies", "账户"],
  ["ai", "AI"],
  ["me", "我的"],
];
export const pageNames = Object.fromEntries([
  ...tabs,
  ["compare", "成绩对比"],
  ["records", "操作记录"],
  ["notifications", "通知"],
  ["diagnostics", "系统状态"],
]);
const preference = {
  get(k) {
    try {
      return localStorage.getItem(k);
    } catch {
      return null;
    }
  },
  set(k, v) {
    try {
      localStorage.setItem(k, v);
    } catch {}
  },
};
const validPage = (v) => (Object.hasOwn(pageNames, v) ? v : "overview");
export const marketViews = [
  ["prices", "金价走势"],
  ["news", "黄金快讯"],
  ["calendar", "经济日历"],
  ["reference", "市场参考"],
];
export const accountViews = [
  ["live", "真实账户"],
  ["simulation", "模拟账户"],
];
export const aiViews = [
  ["research", "最新研究"],
  ["control", "模型控制"],
  ["evaluation", "评估与运行"],
];
export const notificationViews = [
  ["settings", "订阅设置"],
  ["history", "发送记录"],
];
const pick = (choices, value, fallback) =>
  choices.some(([key]) => key === value) ? value : fallback;
const validAccountView = (v) =>
  ["auto", "live", "simulation"].includes(v) ||
  /^live\/(zheshang|minsheng)\/(holdings|advice|orders|records)$/.test(
    v || "",
  ) ||
  /^simulation\/(codex|claude):[a-z_]+$/.test(v || "")
    ? v
    : "auto";
const parseHash = () => {
  try {
    return location.hash
      .replace(/^#\/?/, "")
      .split("/")
      .map(decodeURIComponent);
  } catch {
    return [];
  }
};
const initial = parseHash();
export const currentPage = ref(
  validPage(initial[0] || preference.get("gold-tab")),
);
export const accountView = ref(
  validAccountView(
    initial[0] === "strategies" ? initial.slice(1).join("/") || "auto" : "auto",
  ),
);
export const marketView = ref(
  pick(marketViews, initial[0] === "market" ? initial[1] : null, "prices"),
);
export const aiView = ref(
  pick(aiViews, initial[0] === "ai" ? initial[1] : null, "research"),
);
export const notificationView = ref(
  pick(
    notificationViews,
    initial[0] === "notifications" ? initial[1] : null,
    "settings",
  ),
);
export const navPage = computed(() =>
  ["compare", "records"].includes(currentPage.value)
    ? "strategies"
    : ["notifications", "diagnostics"].includes(currentPage.value)
      ? "me"
      : currentPage.value,
);
export const marketScroll = new Map();
const scrolls = new Map();
export const diagnosticsOpen = ref(false);
export const dataEpoch = ref(0),
  refreshSignal = ref(0);
export const pageView = (page) =>
  page === "overview"
    ? "summary"
    : page === "strategies"
      ? accountView.value
      : page === "market"
        ? marketView.value
        : page === "ai"
          ? aiView.value
          : page === "notifications"
            ? notificationView.value
            : "auto";
const apiView = (page, view) =>
  page === "strategies" && ["auto", "live", "simulation"].includes(view)
    ? view + "-list"
    : view;
export function scrollPosition(page, view = pageView(page)) {
  return page === "market"
    ? marketScroll.get(view) || 0
    : scrolls.get(page + ":" + view) || 0;
}
function saveScroll() {
  const page = currentPage.value,
    view = pageView(page),
    top = window.scrollY || 0;
  if (page === "market") marketScroll.set(view, top);
  else scrolls.set(page + ":" + view, top);
}
export function restoreScroll(page, view = pageView(page)) {
  if (currentPage.value === page && pageView(page) === view)
    window.scrollTo({ top: scrollPosition(page, view) });
}
function routeHash() {
  const page = currentPage.value,
    view = pageView(page);
  return (
    "#" +
    page +
    (["market", "strategies", "ai", "notifications"].includes(page) &&
    view !== "auto"
      ? "/" + view.split("/").map(encodeURIComponent).join("/")
      : "")
  );
}
function rememberRoute() {
  preference.set("gold-tab", currentPage.value);
  history.replaceState(null, "", routeHash());
}
export function selectView(page, view) {
  saveScroll();
  if (page === "market") marketView.value = pick(marketViews, view, "prices");
  else if (page === "strategies") accountView.value = validAccountView(view);
  else if (page === "ai") aiView.value = pick(aiViews, view, "research");
  else if (page === "notifications")
    notificationView.value = pick(notificationViews, view, "settings");
  const changed = currentPage.value !== page;
  currentPage.value = validPage(page);
  rememberRoute();
  if (changed) window.scrollTo({ top: 0 });
}
export const selectMarketView = (view) => selectView("market", view);
export const openBank = (bank, section = "holdings") =>
  selectView("strategies", "live/" + bank + "/" + section);
export const openPortfolio = (key) =>
  selectView("strategies", "simulation/" + key);
export const header = shallowRef(null);
export const historyFilters = reactive({
  kind: "fills",
  model: "all",
  account: "all",
  days: "all",
});
export const pending = reactive({}),
  actionErrors = reactive({});
export const theme = ref(
  ["auto", "light", "dark"].includes(preference.get("gold-theme"))
    ? preference.get("gold-theme")
    : "auto",
);
const entries = new Map();
let epoch = 0;
export function entryFor(page = currentPage.value, view = pageView(page)) {
  const key = page + ":" + view;
  if (!entries.has(key))
    entries.set(
      key,
      shallowReactive({
        key,
        page,
        view,
        data: null,
        etag: "",
        at: 0,
        loading: false,
        error: "",
        request: null,
        requestVersion: 0,
        version: 0,
      }),
    );
  return entries.get(key);
}
export const activeEntry = computed(() => entryFor());
export function navigate(page, filters) {
  saveScroll();
  if (filters) Object.assign(historyFilters, filters);
  currentPage.value = validPage(page);
  rememberRoute();
  window.scrollTo({ top: 0 });
}
window.addEventListener("hashchange", () => {
  saveScroll();
  const route = parseHash(),
    page = validPage(route[0]);
  currentPage.value = page;
  if (page === "market")
    marketView.value = pick(marketViews, route[1], "prices");
  if (page === "strategies")
    accountView.value = validAccountView(route.slice(1).join("/") || "auto");
  if (page === "ai") aiView.value = pick(aiViews, route[1], "research");
  if (page === "notifications")
    notificationView.value = pick(notificationViews, route[1], "settings");
  window.scrollTo({ top: 0 });
});
export function refreshCurrent() {
  refreshSignal.value++;
  return loadPage(currentPage.value, { force: true });
}
export function setTheme(value) {
  theme.value = value;
  preference.set("gold-theme", value);
}
export async function requestJSON(
  url,
  { method = "GET", body, signal, headers = {} } = {},
) {
  const response = await fetch(url, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    signal,
    headers: {
      ...(method === "POST"
        ? { "Content-Type": "application/json", "X-Gold-Control": "1" }
        : {}),
      ...headers,
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (response.status === 304) return { unchanged: true };
  let value;
  try {
    value = await response.json();
  } catch {
    throw new Error("服务器返回异常，请重试");
  }
  if (!response.ok)
    throw new Error(value.error || `请求失败 HTTP ${response.status}`);
  return { value, etag: response.headers.get("ETag") || "" };
}
export async function loadPage(
  page = currentPage.value,
  { view = pageView(page), force = false } = {},
) {
  const entry = entryFor(page, view);
  if (entry.request) {
    if (entry.requestVersion !== entry.version) {
      await entry.request;
      return loadPage(page, { view, force: true });
    }
    return entry.request;
  }
  if (!force && entry.data && Date.now() - entry.at < 10000) {
    if (entry === entryFor()) header.value = entry.data;
    return entry.data;
  }
  const version = entry.version,
    requestEpoch = epoch,
    controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  entry.requestVersion = version;
  entry.loading = true;
  entry.error = "";
  const job = (async () => {
    try {
      const result = await requestJSON(
        `/api/pages/${page}` +
          (apiView(page, view) !== "auto"
            ? "?view=" + encodeURIComponent(apiView(page, view))
            : ""),
        {
          signal: controller.signal,
          headers:
            entry.data && entry.etag ? { "If-None-Match": entry.etag } : {},
        },
      );
      if (version !== entry.version || requestEpoch !== epoch)
        return entry.data;
      if (!result.unchanged) {
        entry.data = markRaw(result.value);
        entry.etag = result.etag;
      }
      entry.at = Date.now();
      if (entry.data && entry === entryFor()) header.value = entry.data;
      return entry.data;
    } catch (error) {
      if (version === entry.version && requestEpoch === epoch)
        entry.error =
          error.name === "AbortError" ? "加载超时，点击重试" : error.message;
      return entry.data;
    } finally {
      clearTimeout(timer);
      entry.loading = false;
      entry.request = null;
    }
  })();
  entry.request = job;
  return job;
}
export async function runAction(key, action) {
  if (pending[key]) return false;
  pending[key] = true;
  actionErrors[key] = "";
  try {
    await action();
    return true;
  } catch (error) {
    actionErrors[key] = error.message;
    return false;
  } finally {
    pending[key] = false;
  }
}
export async function toggleNotification(topic, enabled) {
  const entry = entryFor("notifications", "settings");
  entry.version++;
  return runAction("notify:" + topic, async () => {
    const { value } = await requestJSON("/api/notifications", {
      method: "POST",
      body: { topic, enabled },
    });
    if (value.key !== topic || typeof value.enabled !== "boolean")
      throw new Error("通知设置返回异常，请重试");
    entry.version++;
    entry.etag = "";
    if (entry.data)
      entry.data = markRaw({
        ...entry.data,
        notifications: {
          ...entry.data.notifications,
          subscriptions: entry.data.notifications.subscriptions.map((s) =>
            s.key === topic ? { ...s, enabled: value.enabled } : s,
          ),
        },
      });
  });
}
function clearPages() {
  epoch++;
  dataEpoch.value++;
  for (const entry of entries.values()) {
    entry.version++;
    entry.data = null;
    entry.etag = "";
    entry.at = 0;
  }
}
export async function toggleModel(model, enabled) {
  return runAction("model:" + model, async () => {
    await requestJSON("/api/models/" + model, {
      method: "POST",
      body: { enabled },
    });
    clearPages();
    header.value = null;
    await loadPage(currentPage.value, { force: true });
  });
}
export async function manualAnalysis(model) {
  return runAction("manual:" + model, async () => {
    await requestJSON("/api/models/" + model + "/analyze", {
      method: "POST",
      body: {},
    });
    for (const entry of entries.values())
      if (entry.page === "ai") {
        entry.version++;
        entry.at = 0;
        entry.etag = "";
      }
    await loadPage("ai", { force: true });
  });
}
export async function refreshLive() {
  return runAction("live-refresh", async () => {
    await requestJSON("/api/live-readonly/refresh", {
      method: "POST",
      body: {},
    });
    for (const entry of entries.values())
      if (entry.page === "strategies") {
        entry.version++;
        entry.at = 0;
        entry.etag = "";
      }
    await loadPage("strategies", { force: true });
  });
}
