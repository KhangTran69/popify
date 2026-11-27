// Actual private staging browser role checks. CI-only test certificate is verified
// by verify_staging.py; this context bypasses CSP solely to inject axe.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { writeFileSync } from "node:fs";
const root = process.env.POPIFY_BROWSER_MODULES;
if (!root) throw Error("Set isolated browser modules directory");
const require = createRequire(resolve(root, "package.json"));
const { chromium } = require("playwright");
const axePath = require.resolve("axe-core/axe.min.js");
const base = "https://localhost:8444";
const report = [];
const browser = await chromium.launch({headless:true});
async function signIn(role) {
  const context = await browser.newContext({ignoreHTTPSErrors:true,bypassCSP:true,viewport:{width:1280,height:900}});
  const page = await context.newPage();
  await page.goto(base + "/login");
  await page.getByLabel("Username").fill("ci-staging-" + role);
  await page.getByLabel("Password").fill("synthetic-staging-test-password");
  await Promise.all([page.waitForURL(base + "/"), page.getByRole("button",{name:"Sign in to workspace"}).click()]);
  return {page,context};
}
async function scan(page,name) {
  await page.addScriptTag({path:axePath});
  const result = await page.evaluate(() => window.axe.run({runOnly:{type:"tag",values:["wcag2a","wcag2aa","wcag21a","wcag21aa"]}}));
  const violations = result.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}));
  report.push({name,viewport:page.viewportSize(),violations});
  console.log("MANAGED AXE " + JSON.stringify({name,violations}));
  assert.deepEqual(violations.filter(v=>["critical","serious"].includes(v.impact)),[],name);
}
try {
  const admin = await signIn("admin");
  await admin.page.goto(base + "/operators");
  await scan(admin.page,"admin-operator-accounts");
  await admin.page.setViewportSize({width:390,height:844});
  await scan(admin.page,"mobile-admin-operator-accounts");
  const auditor = await signIn("auditor");
  await auditor.page.goto(base + "/operations");
  assert.equal(await auditor.page.getByRole("link",{name:"Operator accounts"}).count(),0);
  assert.equal(await auditor.page.getByRole("link",{name:"New inquiry"}).count(),0);
  await scan(auditor.page,"auditor-operations");
  assert.equal((await auditor.page.goto(base + "/operators")).status(),403);
  await scan(auditor.page,"auditor-denied");
  await admin.page.setViewportSize({width:1280,height:900});
  const row = admin.page.getByRole("row").filter({hasText:"ci-staging-auditor"});
  await Promise.all([admin.page.waitForURL(base+"/operators"),row.getByRole("button",{name:"Revoke sessions"}).click()]);
  await auditor.page.goto(base+"/operations");
  await auditor.page.waitForURL(base+"/login");
  console.log("PASS: real Chromium staging role visibility, permission error, revocation and managed screen accessibility");
  await admin.context.close();
  await auditor.context.close();
} finally {
  writeFileSync("managed-accessibility-report.json",JSON.stringify(report,null,2));
  await browser.close();
}
