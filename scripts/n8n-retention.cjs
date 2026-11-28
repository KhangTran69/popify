"use strict";
// Bound technical retention before n8n touches the existing workflow volume.
function validateRetention(env) {
  const bounds = { EXECUTIONS_DATA_MAX_AGE: [1, 168], EXECUTIONS_DATA_PRUNE_MAX_COUNT: [100, 10000] };
  for (const [key, [min, max]] of Object.entries(bounds)) {
    const value = env[key] ?? "";
    if (!/^[0-9]+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) < min || Number(value) > max) {
      throw new Error("Invalid bounded n8n retention setting: " + key);
    }
  }
  if (env.EXECUTIONS_DATA_PRUNE !== "true") throw new Error("n8n execution pruning must remain enabled");
}
module.exports = { validateRetention };
if (require.main === module) {
  try { validateRetention(process.env); }
  catch (error) { console.error(error.message); process.exitCode = 2; }
}
