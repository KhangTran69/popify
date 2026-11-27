"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { validateRetention } = require("./n8n-retention.cjs");
const defaults = { EXECUTIONS_DATA_MAX_AGE: "168", EXECUTIONS_DATA_PRUNE_MAX_COUNT: "10000", EXECUTIONS_DATA_PRUNE: "true" };
test("permits default and tighter retention", () => {
  assert.doesNotThrow(() => validateRetention(defaults));
  assert.doesNotThrow(() => validateRetention({ ...defaults, EXECUTIONS_DATA_MAX_AGE: "1", EXECUTIONS_DATA_PRUNE_MAX_COUNT: "100" }));
});
test("rejects disabled, missing, unbounded and malformed retention without exposing values", () => {
  for (const [key, value] of [
    ["EXECUTIONS_DATA_PRUNE", "false"], ["EXECUTIONS_DATA_PRUNE", ""],
    ["EXECUTIONS_DATA_MAX_AGE", "0"], ["EXECUTIONS_DATA_MAX_AGE", "169"],
    ["EXECUTIONS_DATA_MAX_AGE", "1e2"], ["EXECUTIONS_DATA_MAX_AGE", "private-value"],
    ["EXECUTIONS_DATA_PRUNE_MAX_COUNT", "99"], ["EXECUTIONS_DATA_PRUNE_MAX_COUNT", "10001"],
    ["EXECUTIONS_DATA_PRUNE_MAX_COUNT", "999999999999999999999999999999"],
  ]) {
    assert.throws(() => validateRetention({ ...defaults, [key]: value }), error => !error.message.includes("private-value"));
  }
});
