import test from "node:test";
import assert from "node:assert/strict";
import {
  freshness,
  referenceStatus,
  referenceDeadline,
} from "../public-app/presentation.js";

const at = "2026-11-04T00:00:00+08:00";
const now = Date.parse(at);
test("cached market states expire without renewing their timestamp and preserve missing or partial data", () => {
  assert.equal(freshness("fresh", at, 180, now + 180001), "stale");
  assert.equal(freshness("partial", at, 180, now), "partial");
  assert.equal(freshness("unavailable", at, 180, now), "unavailable");
  assert.equal(freshness("fresh", null, 180, now), "unavailable");
  assert.equal(freshness("fresh", at, 180, now - 31000), "stale");
});
test("a reference deadline and its status expire at the same instant, including when refresh fails", () => {
  const report = { analysis: {}, expires_at: at, status: "current" };
  assert.equal(referenceStatus(report, false, now - 1), "current");
  assert.equal(referenceDeadline(at, now - 1), "参考截止");
  assert.equal(referenceStatus(report, true, now), "expired");
  assert.equal(referenceDeadline(at, now), "参考已到期 · 原截止");
  assert.equal(referenceStatus(report, true, now - 1), "refresh_failed");
  assert.equal(
    referenceStatus(
      { ...report, status: "stale", reason: "review_due" },
      false,
      now - 1,
    ),
    "review_due",
  );
  assert.equal(referenceStatus(null, true, now), "unavailable");
});
