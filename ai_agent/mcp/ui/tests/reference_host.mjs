import { chromium } from "playwright"; import fs from "fs";
const DIR = process.argv[2];
const BUNDLE = fs.readFileSync(`${DIR}/refhost/host-bundle.js`, "utf8");
const CSP = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'">`;
const view = f => fs.readFileSync(f, "utf8").replace("<head>", "<head>" + CSP);
const fx = n => JSON.parse(fs.readFileSync(`${DIR}/fx/${n}.json`, "utf8"));
const results = []; const check = (name, ok, extra = "") => { results.push(ok); console.log(ok ? "PASS" : "FAIL", name, ok ? "" : extra); };
const caps = { serverTools: {}, openLinks: {}, downloadFile: {}, updateModelContext: { text: {} }, message: { text: {} }, logging: {} };
const context = { theme: "light", locale: "ar-YE", timeZone: "Asia/Aden", platform: "web", displayMode: "inline", availableDisplayModes: ["inline", "fullscreen"], containerDimensions: { maxHeight: 900, width: 760 } };
const browser = await chromium.launch();
async function run(name, html, toolResult, { late = 0, reply = async () => ({ content: [{ type: "text", text: "ok" }], structuredContent: { ok: true } }), wait = 1600 } = {}) {
  const page = await browser.newPage({ viewport: { width: 800, height: 900 } });
  const errs = []; page.on("pageerror", e => errs.push(e.message)); page.on("console", m => (m.type() === "error" || /Security|schema|invalid/i.test(m.text())) && errs.push(m.text()));
  await page.exposeFunction("hostReply", reply);
  await page.setContent(`<!doctype html><body style="margin:0"><iframe id="f" sandbox="allow-scripts" style="width:760px;border:0"></iframe><script>${BUNDLE.replace(/<\/script/g, "<\\/script")}</script></body>`);
  await page.evaluate(args => window.startHost(args), { html, toolResult, toolInput: {}, caps, context, late });
  await page.waitForTimeout(wait + late);
  const frame = page.frames().find(x => x !== page.mainFrame());
  const out = { log: await page.evaluate(() => window.__log), hostErrors: await page.evaluate(() => window.__errors), calls: await page.evaluate(() => window.__calls || []), ping: await page.evaluate(() => new Promise(res => { const f = document.getElementById("f"); const h = e => { if (e.source === f.contentWindow && e.data && e.data.id === "ping-1") { window.removeEventListener("message", h); res(e.data.error ? "error" : "pong"); } }; window.addEventListener("message", h); f.contentWindow.postMessage({ jsonrpc: "2.0", id: "ping-1", method: "ping" }, "*"); setTimeout(() => res("no answer"), 800); })),
    unknown: await page.evaluate(() => new Promise(res => { const f = document.getElementById("f"); const h = e => { if (e.source === f.contentWindow && e.data && e.data.id === "unk-1") { window.removeEventListener("message", h); res(e.data.error ? e.data.error.code : "result"); } }; window.addEventListener("message", h); f.contentWindow.postMessage({ jsonrpc: "2.0", id: "unk-1", method: "some/unknown" }, "*"); setTimeout(() => res("no answer"), 800); })), errs, frame, page };
  await page.screenshot({ path: `${DIR}/ref_${name}.png`, fullPage: true });
  return out;
}
const text = (f, sel) => f.evaluate(s => document.querySelector(s)?.textContent.trim(), sel);

const MIN = view("/home/snd/frappe-bench/apps/ai_agent/ai_agent/mcp/ui/minimal.html"), MAIN = view(`${DIR}/fx/main.html`);
const probe = { content: [{ type: "text", text: "عدد الشركات: 5" }], structuredContent: { companies: 5, user: "u" }, isError: false };
for (const late of [0, 1500]) {
  const tag = late ? " (host listens late)" : "";
  let r = await run("minimal" + late, MIN, probe, { late });
  check("official host + minimal view: handshake completes" + tag, r.log.includes("initialized") && r.log.includes("result-sent"), JSON.stringify(r.log) + JSON.stringify(r.hostErrors));
  check("official host + minimal view: renders connected + count" + tag, (await text(r.frame, "#status")) === "MCP App connected successfully" && (await text(r.frame, "#count")).includes("5"), await text(r.frame, "#status"));
  check("official host + minimal view: no schema/host errors" + tag, r.hostErrors.length === 0 && r.errs.length === 0, JSON.stringify(r.hostErrors.concat(r.errs)).slice(0, 400));
  check("official host + minimal view: answers ping, refuses unknown requests" + tag, r.ping === "pong" && r.unknown === -32601, r.ping + " / " + r.unknown);
  await r.page.close();
  r = await run("main" + late, MAIN, fx("s2_approval"), { late, reply: async p => p.name === "view__event" ? { content: [{ type: "text", text: "ok" }], structuredContent: { ok: true } } : fx("approval_done") });
  check("official host + main view: handshake completes" + tag, r.log.includes("initialized") && r.log.includes("result-sent"), JSON.stringify(r.log) + JSON.stringify(r.hostErrors));
  check("official host + main view: approval card with buttons" + tag, (await text(r.frame, "h1") || "").includes("فاتورة بيع") && (await r.frame.$$(".ag-foot .btn")).length === 3, await text(r.frame, "h1"));
  check("official host + main view: telemetry accepted by the official schema" + tag, r.log.includes("tools/call:view__event"), JSON.stringify(r.log));
  check("official host + main view: answers ping, refuses unknown requests" + tag, r.ping === "pong" && r.unknown === -32601, r.ping + " / " + r.unknown);
  check("official host + main view: no schema/host errors" + tag, r.hostErrors.length === 0 && r.errs.length === 0, JSON.stringify(r.hostErrors.concat(r.errs)).slice(0, 400));
  if (!late) {
    await r.frame.click(".btn.success"); await r.page.waitForTimeout(500);
    const calls = await r.page.evaluate(() => window.__calls.filter(c => c.name !== "view__event"));
    check("official host + main view: approve button -> approvals__decide passes the official tools/call schema", calls[0]?.name === "approvals__decide" && calls[0].arguments.decision === "approve", JSON.stringify(calls[0]));
    check("official host + main view: result card after approve", (await text(r.frame, ".ag-head .badge") || "").includes("تحقق"));
    const log = await r.page.evaluate(() => window.__log);
    check("official host + main view: update-model-context accepted", log.includes("update-model-context"), JSON.stringify(log));
  }
  await r.page.close();
}
console.log(results.every(Boolean) ? `ALL ${results.length} PASSED` : `${results.filter(x => !x).length} FAILED of ${results.length}`);
await browser.close();
