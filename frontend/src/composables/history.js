import { shallowReactive, markRaw, watch } from "vue";
import { requestJSON, dataEpoch } from "./dashboard.js";
const feeds = new Map(),
  TTL = 10000;
export function historyEntry(filters) {
  const query = {
    kind: filters.kind,
    model: filters.model,
    account: filters.account,
    days: filters.days,
  };
  const key = JSON.stringify(query);
  if (!feeds.has(key)) {
    if (feeds.size >= 16) {
      const [old, e] = feeds.entries().next().value;
      e.version++;
      e.controller?.abort();
      feeds.delete(old);
    }
    feeds.set(
      key,
      shallowReactive({
        key,
        query,
        rows: [],
        loaded: false,
        more: false,
        at: 0,
        busy: false,
        error: "",
        request: null,
        controller: null,
        version: 0,
      }),
    );
  }
  return feeds.get(key);
}
export function clearHistory() {
  for (const e of feeds.values()) {
    e.version++;
    e.controller?.abort();
    e.rows = [];
    e.loaded = false;
    e.at = 0;
    e.more = false;
    e.error = "";
  }
}
watch(dataEpoch, clearHistory, { flush: "sync" });
export async function loadHistory(entry, { more = false, force = false } = {}) {
  if (entry.request) {
    if (entry.requestVersion !== entry.version) {
      await entry.request;
      return loadHistory(entry, { force: true });
    }
    return entry.request;
  }
  if (!more && !force && entry.loaded && Date.now() - entry.at < TTL) return;
  if (more && entry.loaded && !entry.more) return;
  const version = entry.version,
    controller = new AbortController();
  entry.controller = controller;
  entry.requestVersion = version;
  const timer = setTimeout(() => controller.abort(), 15000);
  entry.busy = true;
  entry.error = "";
  const job = (async () => {
    try {
      let rows = [],
        hasMore = true,
        offset = more ? entry.rows.length : 0;
      const target = more ? 20 : Math.max(20, entry.rows.length);
      while (rows.length < target && hasMore) {
        const query = new URLSearchParams({
          ...entry.query,
          compact: "1",
          limit: String(Math.min(100, target - rows.length)),
          offset: String(offset),
        });
        const { value } = await requestJSON("/api/history?" + query, {
          signal: controller.signal,
        });
        if (version !== entry.version) return;
        rows.push(...value.items);
        offset += value.items.length;
        hasMore = value.has_more && value.items.length > 0;
      }
      if (version !== entry.version) return;
      const unique = new Map();
      for (const r of more ? [...entry.rows, ...rows] : rows)
        unique.set(
          [
            entry.query.kind,
            r.source || "",
            r.account || "",
            r.id ?? r.stamp,
          ].join(":"),
          r,
        );
      entry.rows = markRaw([...unique.values()]);
      entry.more = hasMore;
      entry.loaded = true;
      entry.at = Date.now();
    } catch (e) {
      if (version === entry.version)
        entry.error =
          e.name === "AbortError" ? "历史加载超时，请重试" : e.message;
    } finally {
      clearTimeout(timer);
      entry.busy = false;
      entry.request = null;
      entry.controller = null;
    }
  })();
  entry.request = job;
  return job;
}
