// Real Chromium verification against the isolated, synthetic preview.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { writeFileSync } from "node:fs";
import { setTimeout as pause } from "node:timers/promises";
const root = process.env.POPIFY_BROWSER_MODULES;
if (!root) throw Error("Set POPIFY_BROWSER_MODULES to the isolated npm installation directory");
const require = createRequire(resolve(root, "package.json"));
const { chromium } = require("playwright");
const axePath = require.resolve("axe-core/axe.min.js");
const base = "http://127.0.0.1:8002";
const child = spawn(process.env.PYTHON || "python", ["-m", "scripts.preview", "--port", "8002"], {
  stdio: ["ignore", "inherit", "inherit"], env: { ...process.env, POPIFY_ENV: "production", COOKIE_SECURE: "false" }
});
const report = [];
const fatal = new Set(["button-name", "label", "link-name", "aria-valid-attr", "aria-valid-attr-value",
  "duplicate-id-aria", "html-has-lang", "document-title", "landmark-one-main", "color-contrast", "link-in-text-block"]);
let browser;
async function scan(page, name) {
  await page.addScriptTag({ path: axePath });
  const result = await page.evaluate(async () => window.axe.run({
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] }
  }));
  const violations = result.violations.map(v => ({
    id: v.id, impact: v.impact, description: v.description, targets: v.nodes.map(n => n.target)
  }));
  report.push({ name, viewport: page.viewportSize(), violations });
  console.log("AXE " + JSON.stringify({page:name,violations:violations.map(v=>({rule:v.id,impact:v.impact,count:v.targets.length}))}));
  assert.deepEqual(violations.filter(v => fatal.has(v.id)), [], name + ": critical structural accessibility rules");
  assert.equal(await page.locator("main").count(), 1, name + ": one main landmark");
}
try {
  let ready = false;
  for (let i = 0; i < 60; i++) {
    if (child.exitCode !== null) throw Error("Preview exited");
    try { ready = (await fetch(base + "/live")).ok; } catch {}
    if (ready) break;
    await pause(500);
  }
  assert(ready, "Preview did not become ready");
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(base + "/login");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), "Skip to content");
  await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.activeElement.id), "main");
  await scan(page, "login");
  await page.getByLabel("Username").fill("preview");
  await page.getByLabel("Password").fill("preview-only");
  await Promise.all([page.waitForURL(base + "/"), page.getByRole("button", { name: "Sign in to workspace" }).click()]);
  await scan(page, "dashboard");
  for (const route of ["/recovery", "/queues/orders", "/queues/cases", "/queues/approvals", "/support", "/demo"]) {
    await page.goto(base + route);
    await scan(page, route);
  }
  await Promise.all([page.waitForURL(/\/demo\/scenarios\/\d+/), page.getByRole("button", { name: "Start success run" }).click()]);
  const scenario = page.url();
  await scan(page, "pending-scenario");
  await page.getByRole("link", { name: "Review pending decision" }).click();
  await scan(page, "approval");
  await Promise.all([page.waitForURL(scenario), page.getByRole("button", { name: "Approve mock action" }).click()]);
  assert(await page.getByText(/mock action completed|mock execution completed|successfully completed|persisted mock|scenario complete/i).count() > 0,
         "Expected completed mock outcome in scenario");
  await page.setViewportSize({ width: 390, height: 844 });
  await scan(page, "mobile-scenario");
  await page.goto(base + "/recovery");
  await scan(page, "mobile-recovery");
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 2),
         "Recovery page has horizontal overflow at mobile width");
  console.log("PASS: Chromium keyboard navigation, login, representative pages, mock approval and mobile recovery");
} finally {
  writeFileSync(process.env.POPIFY_ACCESSIBILITY_REPORT || "accessibility-report.json", JSON.stringify(report, null, 2));
  if (browser) await browser.close();
  child.kill("SIGTERM");
}
