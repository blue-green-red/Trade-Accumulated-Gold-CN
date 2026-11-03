import { test } from "node:test";
import assert from "node:assert/strict";
import {
  pastDeadline,
  statusLabel,
  validity,
  totals,
  difference,
} from "../src/format.js";
import { clock } from "../src/clock.js";
test("pending orders and no-trade references expire independently of existing protection", () => {
  const plan = {
    status: "pending",
    expires_at: new Date(Date.now() + 60000).toISOString(),
    decision: { action: "buy" },
  };
  clock.value = Date.now();
  assert.equal(pastDeadline(plan), false);
  clock.value = Date.now() + 120000;
  assert.equal(statusLabel(plan), "已到期（未成交）");
  assert.match(validity(plan), /不再执行/);
  const hold = { ...plan, status: "executed", decision: { action: "hold" } };
  assert.equal(statusLabel(hold), "参考已到期（无成交）");
  assert.match(validity(hold), /保护不随/);
});
test("unknown valuations stay unknown and benchmark differences use percentage points", () => {
  assert.equal(
    totals([
      { started: true, budget: 100000, value: null, cash: 0, grams: 100 },
    ]).value,
    null,
  );
  assert.equal(
    totals([
      { started: false, budget: 100000, value: 100000, cash: 100000, grams: 0 },
    ]).budget,
    null,
  );
  assert.equal(difference(0.01), "领先基准 1.00 个百分点");
});

test("cached quotes expire on the local clock without extending either timestamp", async () => {
  const { executionStatus } = await import("../src/format.js");
  const at = Date.now();
  clock.value = at;
  const q = {
    source: "jd_latest_price_api",
    price: 850.12,
    cache_reused: true,
    quote_at: new Date(at - 60000).toISOString(),
    fetched_at: new Date(at - 60000).toISOString(),
  };
  const data = {
    execution: { ready: true, quote: q },
    snapshot: { jd_session: { is_open: true } },
  };
  assert.equal(executionStatus(data).ready, true);
  assert.match(executionStatus(data).label, /沿用缓存/);
  clock.value = at + 61000;
  assert.equal(executionStatus(data).ready, false);
  assert.match(executionStatus(data).label, /超过120秒/);
  clock.value = at;
  data.snapshot.jd_session.close_at = new Date(at - 1).toISOString();
  assert.equal(executionStatus(data).ready, false);
  assert.match(executionStatus(data).label, /休市/);
  clock.value = at;
});
