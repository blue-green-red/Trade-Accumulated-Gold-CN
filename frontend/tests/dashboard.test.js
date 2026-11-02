import { test } from "node:test";
import assert from "node:assert/strict";
globalThis.location = { hash: "#overview" };
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.history = { replaceState: () => {} };
globalThis.window = { addEventListener: () => {}, scrollTo: () => {} };
const { entryFor, loadPage, toggleNotification, toggleModel, currentPage } =
  await import("../src/composables/dashboard.js");
const response = (value) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json", ETag: 'W/"test"' },
  });

test("an older page response cannot undo a successfully saved notification switch", async () => {
  const entry = entryFor("notifications");
  entry.data = {
    notifications: { subscriptions: [{ key: "codex:low", enabled: false }] },
  };
  entry.etag = "";
  entry.at = 0;
  let finish;
  globalThis.fetch = (_url, opts) =>
    opts.method === "POST"
      ? Promise.resolve(response({ key: "codex:low", enabled: true }))
      : new Promise((resolve) => (finish = resolve));
  const loading = loadPage("notifications", { force: true });
  await toggleNotification("codex:low", true);
  assert.equal(entry.data.notifications.subscriptions[0].enabled, true);
  finish(
    response({
      notifications: { subscriptions: [{ key: "codex:low", enabled: false }] },
    }),
  );
  await loading;
  assert.equal(entry.data.notifications.subscriptions[0].enabled, true);
});

test("disabling a model evicts cached data for other pages before revisiting them", async () => {
  const entry = entryFor("strategies");
  entry.data = { leaderboard: [{ key: "claude:low_full" }] };
  currentPage.value = "ai";
  globalThis.fetch = (url, opts) =>
    Promise.resolve(
      response(
        opts.method === "POST"
          ? { enabled: false }
          : {
              page: "ai",
              model_controls: [
                { key: "claude", enabled: false },
                { key: "codex", enabled: true },
              ],
            },
      ),
    );
  await toggleModel("claude", false);
  assert.equal(entry.data, null);
  assert.equal(entryFor("ai").data.model_controls[0].enabled, false);
});

test("failed refresh keeps the previous account data and exposes a retryable error", async () => {
  const entry = entryFor("strategies");
  const original = {
    account_view: "live",
    live_readonly: [{ bank: "zheshang", total_grams: 12.5 }],
  };
  entry.data = original;
  globalThis.fetch = () => Promise.reject(new Error("network unavailable"));
  await loadPage("strategies", { force: true });
  assert.equal(entry.data, original);
  assert.equal(entry.error, "network unavailable");
  assert.equal(entry.loading, false);
});

test("concurrent refreshes share one network request", async () => {
  let count = 0,
    finish;
  globalThis.fetch = () => {
    count++;
    return new Promise((resolve) => (finish = resolve));
  };
  const first = loadPage("market"),
    second = loadPage("market", { force: true });
  finish(response({ page: "market" }));
  await Promise.all([first, second]);
  assert.equal(count, 1);
});

test("revisiting a page waits for an invalidated read and fetches the new model state", async () => {
  currentPage.value = "ai";
  let finishOld,
    reads = 0;
  globalThis.fetch = (url, opts) => {
    if (opts.method === "POST")
      return Promise.resolve(response({ enabled: false }));
    if (url.includes("/strategies")) {
      reads++;
      if (reads === 1) return new Promise((resolve) => (finishOld = resolve));
      return Promise.resolve(
        response({ leaderboard: [{ key: "codex:low_full" }] }),
      );
    }
    return Promise.resolve(
      response({
        page: "ai",
        model_controls: [{ key: "claude", enabled: false }],
      }),
    );
  };
  const oldRead = loadPage("strategies", { force: true });
  await toggleModel("claude", false);
  const revisit = loadPage("strategies");
  finishOld(response({ leaderboard: [{ key: "claude:low_full" }] }));
  await Promise.all([oldRead, revisit]);
  assert.equal(reads, 2);
  assert.equal(
    entryFor("strategies").data.leaderboard[0].key,
    "codex:low_full",
  );
});
