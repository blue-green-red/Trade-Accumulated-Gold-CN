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
  ["strategies", "账户"],
  ["compare", "操盘对比"],
  ["ai", "AI"],
  ["market", "行情"],
  ["records", "操盘记录"],
  ["notifications", "通知"],
];
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
const validPage = (v) => (tabs.some(([k]) => k === v) ? v : "overview");
export const marketViews = [
  ["prices", "金价走势"],
  ["news", "黄金快讯"],
  ["calendar", "经济日历"],
  ["reference", "市场参考"],
];
const validMarketView = (v) =>
  marketViews.some(([key]) => key === v) ? v : "prices";
const route = location.hash.replace(/^#\/?/, "").split("/");
export const currentPage = ref(
  validPage(route[0] || preference.get("gold-tab")),
);
export const accountView = ref("auto");
export const marketView = ref(
  validMarketView(route[0] === "market" ? route[1] : null),
);
export const marketScroll = new Map();
export const diagnosticsOpen = ref(false);
const pageView = (page) =>
  page === "strategies"
    ? accountView.value
    : page === "market"
      ? marketView.value
      : "auto";
function saveMarketScroll() {
  if (currentPage.value === "market")
    marketScroll.set(marketView.value, window.scrollY || 0);
}
function routeHash() {
  return (
    "#" +
    currentPage.value +
    (currentPage.value === "market" ? "/" + marketView.value : "")
  );
}
export function selectMarketView(view) {
  saveMarketScroll();
  marketView.value = validMarketView(view);
  history.replaceState(null, "", routeHash());
}
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
  saveMarketScroll();
  if (filters) Object.assign(historyFilters, filters);
  currentPage.value = validPage(page);
  preference.set("gold-tab", currentPage.value);
  history.replaceState(null, "", routeHash());
  window.scrollTo({ top: 0 });
}
window.addEventListener("hashchange", () => {
  saveMarketScroll();
  const route = location.hash.replace(/^#\/?/, "").split("/");
  currentPage.value = validPage(route[0]);
  if (route[0] === "market") marketView.value = validMarketView(route[1]);
  window.scrollTo({ top: 0 });
});
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
  if (!force && entry.data && Date.now() - entry.at < 10000) return entry.data;
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
          (["strategies", "market"].includes(page)
            ? "?view=" + encodeURIComponent(view)
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
  const entry = entryFor("notifications");
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
    entryFor("ai").version++;
    await loadPage("ai", { force: true });
  });
}
export async function refreshLive() {
  return runAction("live-refresh", async () => {
    await requestJSON("/api/live-readonly/refresh", {
      method: "POST",
      body: {},
    });
    entryFor("strategies", accountView.value).version++;
    await loadPage("strategies", { force: true });
  });
}
