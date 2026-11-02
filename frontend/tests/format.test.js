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
