// Only public responses enter this cache. Nothing requests a model or private API.
export function createFeedCache(fetcher = globalThis.fetch, now = Date.now) {
  const entries = new Map();
  function check(url) {
    if (!/^\/api\/public\/(?:ai|market)(?:\?|$)/.test(url))
      throw new Error("Unsupported feed");
  }
  return {
    peek(url) {
      check(url);
      return entries.get(url)?.data;
    },
    read(url, { ttl = 30000, force = false } = {}) {
      check(url);
      let entry = entries.get(url);
      if (entry?.pending) return entry.pending;
      if (entry?.data && !force && now() - entry.at < ttl)
        return Promise.resolve(entry.data);
      if (!entry) {
        entry = {};
        entries.set(url, entry);
      }
      entry.pending = (async () => {
        const response = await fetcher(url, {
          credentials: "omit",
          cache: "no-cache",
          signal: AbortSignal.timeout(12000),
          headers: entry.etag ? { "If-None-Match": entry.etag } : {},
        });
        if (response.status === 304 && entry.data) {
          entry.at = now();
          return entry.data;
        }
        if (!response.ok) throw new Error("数据暂时无法更新");
        const data = await response.json();
        entry.data = data;
        entry.at = now();
        entry.etag = response.headers.get("ETag");
        return data;
      })().finally(() => {
        entry.pending = null;
      });
      return entry.pending;
    },
  };
}
export const feeds = createFeedCache();
