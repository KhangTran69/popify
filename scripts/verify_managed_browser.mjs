// Actual private staging browser role checks. CI-only test certificate is verified
// by verify_staging.py. Keep CSP enabled for all application interactions.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { writeFileSync, readFileSync } from "node:fs";
import { setTimeout as pause } from "node:timers/promises";
const root = process.env.POPIFY_BROWSER_MODULES;
if (!root) throw Error("Set isolated browser modules directory");
const require = createRequire(resolve(root, "package.json"));
const { chromium } = require("playwright");
const axeSource = readFileSync(require.resolve("axe-core/axe.min.js"),"utf8");
const base = "https://localhost:8444";
const report = [];
const browser = await chromium.launch({headless:true});
async function signIn(role) {
  const context = await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1280,height:900}});
  const page = await context.newPage();
  await page.goto(base + "/login");
  await page.getByLabel("Username").fill("ci-staging-" + role);
  await page.getByLabel("Password").fill("synthetic-staging-test-password");
  await Promise.all([page.waitForURL(base + "/"), page.getByRole("button",{name:"Sign in to workspace"}).click()]);
  return {page,context};
}
async function scan(page,name) {
  // DevTools evaluation instruments the page without relaxing its CSP for app scripts.
  await page.evaluate(axeSource);
  const result = await page.evaluate(() => window.axe.run({runOnly:{type:"tag",values:["wcag2a","wcag2aa","wcag21a","wcag21aa"]}}));
  const violations = result.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}));
  report.push({name,viewport:page.viewportSize(),violations});
  console.log("MANAGED AXE " + JSON.stringify({name,violations}));
  assert.deepEqual(violations.filter(v=>["critical","serious"].includes(v.impact)),[],name);
}
async function waitFor(page, condition, label) {
  const deadline = Date.now() + 100_000;
  while (Date.now() < deadline) {
    if (await condition()) return;
    await pause(2000);
    await page.reload();
  }
  throw Error("Scheduled workflow did not reach " + label);
}
async function queueInquiry(page, message) {
  await page.goto(base + "/support");
  const csrf = await page.locator('form[action="/support"] input[name="csrf_token"]').inputValue();
  await page.getByLabel("Customer message").fill(message);
  await Promise.all([page.waitForURL(/\/cases\/\d+/), page.getByRole("button",{name:"Queue inquiry"}).click()]);
  return {url:page.url(),csrf};
}
async function findApproval(page, caseUrl) {
  await page.goto(caseUrl);
  await waitFor(page, async()=>await page.locator("#related-approvals a").count() > 0, "pending support approval");
  return new URL(await page.locator("#related-approvals a").first().getAttribute("href"),base).href;
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
  const support = await signIn("support");
  await support.page.goto(base+"/support");
  await scan(support.page,"managed-support-form");
  await support.page.setViewportSize({width:390,height:844});
  await scan(support.page,"mobile-managed-support-form");
  assert(await support.page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2),"Mobile support form overflows");
  await support.page.setViewportSize({width:1280,height:900});
  const approved = await queueInquiry(support.page,"Synthetic staging inquiry approved");
  const rejected = await queueInquiry(support.page,"Synthetic staging inquiry rejected");
  // Exercise the actual app refresh script with its strict CSP intact.
  await Promise.all([support.page.waitForNavigation(),support.page.getByRole("button",{name:"Refresh status"}).click()]);
  const approveUrl = await findApproval(support.page,approved.url);
  const rejectUrl = await findApproval(support.page,rejected.url);
  await support.page.goto(approveUrl);
  assert.equal(await support.page.getByRole("button",{name:"Approve mock action"}).count(),0);
  assert.equal(await support.page.getByRole("button",{name:"Reject",exact:true}).count(),0);
  const forbidden = await support.page.request.post(new URL(new URL(approveUrl).pathname+"/decision",base).href,
    {form:{decision:"approved",csrf_token:approved.csrf},maxRedirects:0});
  assert.equal(forbidden.status(),403);
  await scan(support.page,"managed-support-readonly-approval");
  const reviewer = await signIn("reviewer");
  assert.equal((await reviewer.page.goto(base+"/support")).status(),403);
  await reviewer.page.goto(rejectUrl);
  await scan(reviewer.page,"managed-reviewer-pending");
  await Promise.all([reviewer.page.waitForNavigation(),reviewer.page.getByRole("button",{name:"Reject",exact:true}).click()]);
  assert(await reviewer.page.getByText(/Rejected by ci-staging-reviewer/).count()>0);
  await scan(reviewer.page,"managed-reviewer-rejected");
  await reviewer.page.goto(approveUrl);
  await Promise.all([reviewer.page.waitForNavigation(),reviewer.page.getByRole("button",{name:"Approve mock action"}).click()]);
  await waitFor(reviewer.page,async()=>await reviewer.page.getByText("Mock action completed.",{exact:false}).count()>0,"mock action completion");
  await scan(reviewer.page,"managed-reviewer-completed");
  await support.context.close();
  await reviewer.context.close();
  console.log("PASS: real Chromium staging role visibility, permission error, revocation and managed screen accessibility; scheduled support triage, reviewer rejection and approved mock execution");
  await admin.context.close();
  await auditor.context.close();
} finally {
  writeFileSync("managed-accessibility-report.json",JSON.stringify(report,null,2));
  await browser.close();
}
