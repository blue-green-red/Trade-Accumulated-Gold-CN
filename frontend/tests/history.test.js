import { test } from "node:test";
import assert from "node:assert/strict";
globalThis.location = { hash: "#records" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.history = { replaceState: () => {} };
globalThis.window = { addEventListener: () => {}, scrollTo: () => {} };
const { historyEntry, loadHistory, clearHistory } =
  await import("../src/composables/history.js");
const { dataEpoch } = await import("../src/composables/dashboard.js");
const response = (value) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
const filter = (account = "all") => ({
  kind: "fills",
  model: "all",
  account,
  days: "all",
});
const rows = (start, count) =>
  Array.from({ length: count }, (_, i) => ({
    id: start + i,
    account: "codex:low_empty",
    source: "ai",
  }));
test("feed caches coalesce requests, retain loaded pages on refresh, and isolate filters", async () => {
  const entry = historyEntry(filter("pagination"));
  let reads = 0;
  globalThis.fetch = async (url) => {
    reads++;
    const q = new URL(url, "https://test").searchParams;
    return response({
      items: rows(Number(q.get("offset")), Number(q.get("limit"))),
      has_more: true,
    });
  };
  await Promise.all([loadHistory(entry), loadHistory(entry)]);
  assert.equal(reads, 1);
  await loadHistory(entry, { more: true });
  assert.equal(entry.rows.length, 40);
  await loadHistory(entry);
  assert.equal(reads, 2);
  await loadHistory(entry, { force: true });
  assert.equal(entry.rows.length, 40);
  assert.equal(reads, 3);
  const other = historyEntry(filter("different"));
  assert.equal(other.rows.length, 0);
  assert.equal(historyEntry(filter("pagination")), entry);
});
test("model invalidation clears rows immediately and rejects an old in-flight history result", async () => {
  const entry = historyEntry(filter("generation"));
  entry.rows = rows(0, 1);
  let finish,
    reads = 0;
  globalThis.fetch = async () => {
    reads++;
    return reads === 1
      ? new Promise((resolve) => (finish = resolve))
      : response({
          items: [{ id: 2, account: "codex:low_empty" }],
          has_more: false,
        });
  };
  const stale = loadHistory(entry, { force: true });
  dataEpoch.value++;
  assert.equal(entry.rows.length, 0);
  const next = loadHistory(entry);
  finish(
    response({
      items: [{ id: 1, account: "claude:low_empty" }],
      has_more: false,
    }),
  );
  await Promise.all([stale, next]);
  assert.equal(reads, 2);
  assert.deepEqual(entry.rows, [{ id: 2, account: "codex:low_empty" }]);
});
test("a failed feed refresh keeps previous rows and allows retry", async () => {
  const entry = historyEntry(filter("failure"));
  entry.rows = rows(0, 2);
  globalThis.fetch = async () => {
    throw Error("offline");
  };
  await loadHistory(entry, { force: true });
  assert.equal(entry.rows.length, 2);
  assert.equal(entry.error, "offline");
  assert.equal(entry.busy, false);
  globalThis.fetch = async () =>
    response({ items: rows(5, 2), has_more: false });
  await loadHistory(entry, { force: true });
  assert.equal(entry.error, "");
  assert.equal(entry.rows[0].id, 5);
  clearHistory();
  assert.equal(entry.rows.length, 0);
});
