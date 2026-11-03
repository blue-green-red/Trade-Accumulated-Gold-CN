import test from "node:test";
import assert from "node:assert/strict";
import { createFeedCache } from "../public-app/cache.js";

test("public feeds coalesce reads, cache by route, revalidate with ETags and omit credentials", async () => {
  let now = 0,
    calls = [];
  const cache = createFeedCache(
    async (url, options) => {
      calls.push({ url, options });
      return calls.length === 2
        ? { status: 304 }
        : {
            ok: true,
            status: 200,
            headers: { get: () => '"public-v1"' },
            json: async () => ({ value: url }),
          };
    },
    () => now,
  );
  const url = "/api/public/ai?model=primary";
  const [a, b] = await Promise.all([cache.read(url), cache.read(url)]);
  assert.equal(calls.length, 1);
  assert.equal(a, b);
  await cache.read(url);
  assert.equal(calls.length, 1);
  now = 30001;
  assert.equal(await cache.read(url), a);
  assert.equal(calls[1].options.headers["If-None-Match"], '"public-v1"');
  assert.equal(calls[0].options.credentials, "omit");
  await cache.read("/api/public/market?view=quote");
  assert.equal(calls.length, 3);
  assert.throws(() => cache.read("/api/dashboard"));
  assert.throws(() => cache.read("/api/public/ai/../../dashboard"));
});

test("failed refresh retains the last public response and chart ranges cache independently", async () => {
  let fail = false,
    count = 0;
  const cache = createFeedCache(async (url) => {
    count++;
    if (fail) throw new Error("offline");
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => ({ url }),
    };
  });
  const url = "/api/public/market?view=history&range=7d";
  const old = await cache.read(url);
  fail = true;
  await assert.rejects(cache.read(url, { force: true }));
  assert.equal(cache.peek(url), old);
  fail = false;
  await cache.read("/api/public/market?view=history&range=30d");
  await cache.read(url);
  assert.equal(count, 3);
});
