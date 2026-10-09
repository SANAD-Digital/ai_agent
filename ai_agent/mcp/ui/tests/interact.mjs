import { chromium } from "playwright";
import fs from "fs";
const DIR = process.argv[2];
const VIEW = fs.readFileSync(`${DIR}/fx/main.html`, "utf8");
const fx = n => JSON.parse(fs.readFileSync(`${DIR}/fx/${n}.json`, "utf8"));
const results = []; const check = (name, ok, extra = "") => { results.push(ok); console.log(ok ? "PASS" : "FAIL", name, ok ? "" : extra); };
const DEFAULT_CAPS = { serverTools: {}, openLinks: {}, downloadFile: {}, updateModelContext: { text: {} }, message: { text: {} } };
const hostPage = (fixture, caps, width, sendResult = true) => `<!doctype html><body style="margin:0"><iframe id="f" style="width:${width}px;border:0" scrolling="no"></iframe><script>
const fixture = ${JSON.stringify(fixture)}; const caps = ${JSON.stringify(caps)};
const f = document.getElementById("f"); f.srcdoc = ${JSON.stringify(VIEW).replace(/<\//g, "<\\/")};
window.__calls = []; window.__reqs = []; window.__events = [];
const ctx = {theme:"light", locale:"ar-YE", timeZone:"Asia/Aden", platform:${width < 600 ? '"mobile"' : '"web"'}, availableDisplayModes:["inline","fullscreen"], containerDimensions:{width:${width}, maxHeight:900}};
window.addEventListener("message", async e => { const m = e.data; if (!m || m.jsonrpc !== "2.0") return;
  const reply = r => f.contentWindow.postMessage({jsonrpc:"2.0", id:m.id, result:r}, "*");
  if (m.method) window.__reqs.push({method:m.method, params:m.params});
  if (m.method === "ui/initialize") reply({protocolVersion:"2026-01-26", hostInfo:{name:"mock",version:"1"}, hostCapabilities:caps, hostContext:ctx});
  else if (m.method === "ui/notifications/initialized") { if (${sendResult}) f.contentWindow.postMessage({jsonrpc:"2.0", method:"ui/notifications/tool-result", params:fixture}, "*"); }
  else if (m.method === "ui/notifications/size-changed") { if (m.params.height) f.style.height = m.params.height + "px"; }
  else if (m.method === "tools/call" && m.params.name === "view__event") { window.__events.push(m.params.arguments); reply({content:[{type:"text",text:"ok"}], isError:false}); }
  else if (m.method === "tools/call") { window.__calls.push(m.params); reply(await window.hostReply(m.params)); }
  else if (m.method === "ui/request-display-mode") reply({mode:m.params.mode});
  else if (m.id != null) reply({}); });
</script></body>`;
const browser = await chromium.launch();
async function open(fixture, { caps = DEFAULT_CAPS, width = 1000, sendResult = true, reply = async () => ({ isError: true, content: [{ type: "text", text: "no reply" }] }), init } = {}) {
  const page = await browser.newPage({ viewport: { width, height: 900 } });
  const errs = []; page.on("pageerror", e => errs.push(e.message)); page.on("console", m => m.type() === "error" && errs.push(m.text()));
  await page.exposeFunction("hostReply", reply);
  await page.setContent(hostPage(fixture, caps, width, sendResult));
  const frame = () => page.frames().find(x => x !== page.mainFrame());
  await page.waitForTimeout(600);
  if (init) await init(frame());
  return { page, frame, errs, events: () => page.evaluate(() => window.__events), calls: () => page.evaluate(() => window.__calls), reqs: () => page.evaluate(() => window.__reqs), shot: p => page.screenshot({ path: `${DIR}/${p}.png`, fullPage: true }) };
}
const txt = (f, sel) => f.evaluate(s => document.querySelector(s)?.textContent.trim(), sel);
const openMenu = async (f, item) => { await f.click(".ag-toolbar .menu button"); await f.click(`.menu .pop button:has-text('${item}')`); };

// 1. server-side table: sort / search / filters / paging all go through view__page, never the model
{
  const t = await open(fx("srv1"), { reply: async () => fx("srv2") }); let f = t.frame();
  check("server table: pager shows the window and the total", (await txt(f, ".pager")).includes("128"), await txt(f, ".pager"));
  await f.click("th:has-text('الإجمالي') button"); await t.page.waitForTimeout(300);
  let c = await t.calls(); check("sort -> view__page sort_key/sort_dir + signed token", c[0]?.name === "view__page" && c[0].arguments.sort_key === "grand_total" && c[0].arguments.sort_dir === "asc" && !!c[0].arguments.token, JSON.stringify(c[0]));
  f = t.frame(); await f.click(".pages .pg:has-text('التالي')"); await t.page.waitForTimeout(300);
  c = await t.calls(); check("next page -> offset advanced", c[1]?.arguments.offset >= 25, JSON.stringify(c[1]));
  check("pager offers numbered pages", (await f.$$(".pages .pg")).length >= 4);
  await f.fill(".search input", "حمود"); await f.press(".search input", "Enter"); await t.page.waitForTimeout(300);
  c = await t.calls(); check("search Enter -> server search with search_key", c[2]?.arguments.search === "حمود" && !!c[2].arguments.search_key, JSON.stringify(c[2]));
  await f.click("button:has-text('الفلاتر')"); await f.selectOption(".filterpanel select >> nth=0", "Paid"); await f.click("button:has-text('تطبيق')"); await t.page.waitForTimeout(300);
  await f.fill(".daterange input[aria-label='من تاريخ']", "2026-09-01"); await t.page.waitForTimeout(300);
  check("keyboard focus stays in the date field after the table reloads", await f.evaluate(() => document.activeElement?.getAttribute("aria-label") === "من تاريخ"));
  await f.fill(".daterange input[aria-label='إلى تاريخ']", "2026-09-30"); await t.page.waitForTimeout(300);
  c = await t.calls(); const fl = c[c.length - 1]?.arguments.filters || [];
  check("filters -> equality + date range sent to the server", fl.some(x => x.field === "status" && x.operator === "=" && x.value === "Paid") && fl.some(x => x.operator === ">=") && fl.some(x => x.operator === "<="), JSON.stringify(fl));
  check("active filters visible as removable chips", (await f.$$(".filterbar .chip.on")).length >= 1);
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.shot("i_server"); await t.page.close();
}
// 2. client table: filter/search/sort/expand/menu without any tool call
{
  const t = await open(fx("wide")); let f = t.frame();
  check("wide rows fold extra columns behind an expander", (await f.$$("td.expander button")).length > 0);
  await f.click("td.expander button >> nth=0"); check("expand shows detail row", (await f.$$("tr.detail")).length === 1);
  await f.click("button:has-text('الفلاتر')"); await f.selectOption(".filterpanel select >> nth=0", "Overdue"); await f.click("button:has-text('تطبيق')");
  const rows = await f.$$eval("tbody tr:not(.detail)", r => r.length);
  check("client filter narrows rows without tool calls", rows > 0 && rows < 9 && (await t.calls()).length === 0, `${rows}`);
  await f.click(".filterbar .chip.on"); check("chip removal restores rows", (await f.$$eval("tbody tr:not(.detail)", r => r.length)) === 9);
  await f.fill(".search input", "zzzz-none"); check("no match -> specific empty state with clear action", !!(await f.$(".empty button")) && (await txt(f, ".empty b")).length > 0);
  await f.click(".empty button"); check("clear action restores rows", (await f.$$eval("tbody tr:not(.detail)", r => r.length)) === 9);
  await f.click("th:has-text('الإجمالي') button");
  const vals = async () => (await f.evaluate(() => { const ths = [...document.querySelectorAll("thead th")]; const i = ths.findIndex(t => t.textContent.includes("الإجمالي")) + 1;
    return [...document.querySelectorAll("tbody tr:not(.detail):not(.kind-group)")].map(r => parseFloat(r.querySelector("td:nth-child(" + i + ") .num").textContent.replace(/,/g, ""))); }));
  let asc = await vals(); check("client sort ascending is numeric (not lexical)", asc.length === 9 && asc.every((v, i) => i === 0 || asc[i - 1] <= v), JSON.stringify(asc));
  await f.click("th:has-text('الإجمالي') button"); let desc = await vals(); check("second click sorts descending", desc.every((v, i) => i === 0 || desc[i - 1] >= v), JSON.stringify(desc));
  await openMenu(f, "تصدير CSV"); await t.page.waitForTimeout(200);
  const dl = (await t.reqs()).find(r => r.method === "ui/download-file");
  check("CSV export (options menu) uses ui/download-file with an embedded resource", dl && dl.params.contents[0].resource.mimeType === "text/csv" && dl.params.contents[0].resource.text.includes("الفاتورة"));
  await openMenu(f, "ملء الشاشة"); await t.page.waitForTimeout(200);
  check("fullscreen uses ui/request-display-mode", (await t.reqs()).some(r => r.method === "ui/request-display-mode" && r.params.mode === "fullscreen"));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.shot("i_wide"); await t.page.close();
}
// 3. companies: index follows the screen, header action opens the desk, honest capital
{
  const t = await open(fx("s1_companies"), { width: 760 }); let f = t.frame();
  check("companies: title, icon, primary 'شركة جديدة' beside the title", (await txt(f, "h1")) === "الشركات" && !!(await f.$(".ag-head .btn.primary")));
  await f.click(".ag-head .btn.primary"); await t.page.waitForTimeout(200);
  check("new-company button opens the desk through ui/open-link", (await t.reqs()).some(r => r.method === "ui/open-link" && r.params.url.includes("/app/company/new")));
  const idx = await f.$$eval("td.rank", els => els.map(e => e.textContent.trim()));
  check("# column is the on-screen position (1..n, whatever the sort)", idx.length > 0 && idx.join() === idx.map((_, i) => i + 1).join(), idx.join());
  check("unrecorded capital reads 'غير مسجل', never 0", (await f.$$eval("tbody tr", r => r.map(x => x.textContent))).every(x => x.includes("غير مسجل")));
  check("no cross-currency notice when no amounts are shown", (await f.$$(".note")).length === 0);
  await t.page.close();
}
// 4. statement: tabs, KPI cards with icons, LTR numbers inside Arabic sentences, actions
{
  const t = await open(fx("s3_statement"), { width: 760, reply: async () => fx("s3_statement") }); let f = t.frame();
  check("statement: four KPI cards with icons", (await f.$$(".kpi .ic")).length === 4);
  check("statement: number + currency stay LTR inside the warning", (await f.$$eval(".note .num", e => e.map(x => x.textContent))).some(x => x.includes("210,000 YER")));
  const all = await f.$$eval("tbody tr", r => r.length);
  await f.click(".tab:has-text('المدفوعات')");
  const pay = await f.$$eval("tbody tr", r => r.length);
  check("tab 'المدفوعات' keeps only payment rows", pay < all && pay >= 1, `${all} -> ${pay}`);
  check("tabs expose the selected state for assistive tech", (await f.$eval(".tab[aria-selected=true]", e => e.textContent)) === "المدفوعات");
  await f.click(".tab:has-text('الكل')");
  await f.click("button:has-text('عرض الفواتير المتأخرة')"); await t.page.waitForTimeout(300);
  check("overdue action drills through the app, not the model", (await t.calls())[0]?.name === "data__query");
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
}
// 5. sales analysis: KPI cards, chart + ranking side by side, computed insight
{
  const t = await open(fx("s4_sales"), { width: 900 }); let f = t.frame();
  check("sales: 4 KPI cards, compact headline number", (await f.$$(".kpi")).length === 4 && (await txt(f, ".kpi .v")).includes("4.8M"));
  check("sales: chart bars and ranking table are both visible", (await f.$$("svg rect")).length === 5 && (await f.$$("tbody tr")).length === 5);
  check("sales: finding card with observation, cause, impact and action", (await txt(f, ".finding p")).includes("51%") && (await f.$$(".finding-dl > div")).length === 3);
  const twoCol = await f.evaluate(() => { const c = document.querySelector(".ag-two"); return getComputedStyle(c).gridTemplateColumns.split(" ").length; });
  check("sales: two-column layout on a wide container", twoCol === 2, String(twoCol)); await t.page.close();
}
// 6. approval: ready card, edit (revise), approve (decide), cancel needs confirmation, model context
{
  const t = await open(fx("s2_approval"), { width: 760, reply: async p => p.name === "approvals__revise" ? fx("approval_revised") : fx("approval_done") }); let f = t.frame();
  check("ready card: approve, reject and edit, facts grid", !!(await f.$(".btn.success")) && !!(await f.$(".ag-foot .btn:has-text('رفض')")) && !!(await f.$(".ag-foot .btn:has-text('تعديل')")) && (await f.$$(".fact")).length >= 5);
  check("ready card: six-step lifecycle visible, review done, approval active", (await f.$$(".step")).length === 6 && (await txt(f, ".step.active")).includes("موافقة") && (await f.$$(".step.done")).length === 3);
  await f.click("button:has-text('تعديل')"); await (await f.$("input[type=number] >> nth=0")).fill("120"); await f.click("button:has-text('حفظ وإعادة التحقق')"); await t.page.waitForTimeout(300);
  let c = await t.calls(); check("edit -> approvals__revise with only the changed field and a token", c[0]?.name === "approvals__revise" && JSON.stringify(c[0].arguments.changes.items) === JSON.stringify({ 1: { qty: "120" } }) && !!c[0].arguments.token && !!c[0].arguments.approval_id, JSON.stringify(c[0]));
  await f.click(".btn.success"); await t.page.waitForTimeout(400);
  c = await t.calls(); check("approve -> approvals__decide with an action-bound token", c[1]?.name === "approvals__decide" && c[1].arguments.decision === "approve" && !!c[1].arguments.token, JSON.stringify(c[1]));
  check("result card: verified state, document link, lifecycle strip", (await txt(f, ".ag-head .badge")).includes("تحقق") && (await txt(f, ".outcome")).includes("ACC-SINV-2026-777") && (await f.$$(".step")).length === 6);
  check("outcome reaches the model through ui/update-model-context", (await t.reqs()).some(r => r.method === "ui/update-model-context" && r.params.content[0].text.includes("ACC-SINV-2026-777")));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
  const u = await open(fx("s2_approval"), { reply: async () => fx("approval_done") }); f = u.frame();
  await f.click(".ag-foot .btn:has-text('رفض')"); check("reject asks for confirmation first, saying what it means", (await u.calls()).length === 0 && !!(await f.$(".confirm[role=alertdialog] button:has-text('تأكيد الرفض')")));
  await f.click(".confirm button:has-text('تراجع')"); check("the confirmation can be dismissed", !(await f.$(".confirm")) && (await u.calls()).length === 0);
  await f.click(".ag-foot .btn:has-text('رفض')"); await f.click("button:has-text('تأكيد الرفض')"); await u.page.waitForTimeout(300); check("confirmed cancel sends reject", (await u.calls())[0]?.arguments.decision === "reject"); await u.page.close();
}
// 6b. four-eyes: your own request has no approve button, but edit and cancel remain
{
  const t = await open(fx("s2_foureyes"), { width: 760 }); const f = t.frame();
  check("four-eyes: no approve button, edit and reject still available", !(await f.$(".btn.success")) && !!(await f.$(".ag-foot .btn:has-text('رفض')")) && !!(await f.$(".ag-foot .btn:has-text('تعديل')")));
  check("four-eyes: the reason is stated", (await txt(f, ".note")).includes("العينين"));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
}
// 7. insufficient stock: blocked card, other-warehouse flow, view stock
{
  const t = await open(fx("s5_blocked"), { width: 760, reply: async p => p.name === "approvals__revise" ? fx("approval_revised") : fx("company") }); let f = t.frame();
  check("blocked: red title, shortage numbers, no approve button", (await txt(f, ".ag-title h1")).includes("لا يمكن اعتماد") && (await txt(f, ".blocked .big")).includes("60") && !(await f.$(".btn.success")));
  check("blocked: four actions, primary is 'عرض المخزون'", (await f.$$(".ag-foot .btn")).length === 4 && (await txt(f, ".ag-foot .btn.primary")).includes("عرض المخزون"));
  await f.click("button:has-text('اختيار مخزن آخر')");
  check("blocked: alternatives listed with available quantity", (await f.$$(".altlist .opt")).length === 2 && (await txt(f, ".altlist .opt .meta")).includes("420"));
  check("blocked: use-warehouse disabled until one is chosen", await f.$eval("button:has-text('استخدام هذا المخزن')", e => e.disabled));
  await f.click(".altlist .opt >> nth=0"); await f.click("button:has-text('استخدام هذا المخزن')"); await t.page.waitForTimeout(300);
  const c = await t.calls(); check("choosing a warehouse -> approvals__revise with set_warehouse", c[0]?.name === "approvals__revise" && c[0].arguments.changes.fields.set_warehouse === "مخزن الفرع الثاني - SND", JSON.stringify(c[0]));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
  const v = await open(fx("s5_blocked"), { width: 760, reply: async () => fx("company") }); f = v.frame();
  await f.click("button:has-text('عرض المخزون')"); await v.page.waitForTimeout(300);
  check("view stock drills to erp__stock_balance", (await v.calls())[0]?.name === "erp__stock_balance", JSON.stringify((await v.calls())[0])); await v.page.close();
}
// 8. choice card: pick, continue -> the answer is sent as the user's message; text host gets a copyable reply
{
  const t = await open(fx("s6_choice"), { width: 760 }); let f = t.frame();
  check("choice: continue is disabled until an option is picked", await f.$eval(".ag-foot .btn.primary", e => e.disabled));
  await f.click(".opt >> nth=1"); check("choice: radio semantics", (await f.$eval(".opt[aria-checked=true]", e => e.getAttribute("role"))) === "radio");
  await f.click(".ag-foot .btn.primary"); await t.page.waitForTimeout(300);
  const msg = (await t.reqs()).find(r => r.method === "ui/message");
  check("choice: selection goes back as a user message (ui/message)", msg && msg.params.content[0].text.startsWith("اخترت الشركة:"), JSON.stringify(msg));
  check("choice: options are locked after sending", (await f.$$eval(".opt", o => o.every(x => x.disabled))));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
  const n = await open(fx("s6_choice"), { width: 760, caps: { serverTools: {} } }); f = n.frame();
  await f.click(".opt >> nth=0"); await f.click(".ag-foot .btn.primary"); await n.page.waitForTimeout(300);
  check("choice without ui/message: no crash, user told to paste the reply", !(await n.reqs()).some(r => r.method === "ui/message") && n.errs.length === 0); await n.page.close();
}
// 9. drill / error / retry / capability fallbacks / chart / group / mobile / empty
{
  let n = 0;
  const t = await open(fx("company"), { reply: async () => (++n === 1 ? { isError: true, content: [{ type: "text", text: "{\"error\":\"Tool execution failed.\"}" }] } : fx("document")) }); let f = t.frame();
  await f.click("tbody tr >> nth=0 >> .lnk"); await t.page.waitForTimeout(300);
  check("drill error shows an error state with retry", (await f.$$(".errorbox")).length === 1 && !!(await f.$(".errorbox button")));
  await f.click(".errorbox button"); await t.page.waitForTimeout(300);
  check("retry re-calls the tool and renders the document card", (await t.calls()).length === 2 && (await f.$$("dl.dl")).length > 0);
  await f.click(".ag-back"); await t.page.waitForTimeout(200); check("back returns to the list", (await txt(f, "h1")) === "الشركات"); await t.page.close();
  const a = await open(fx("srv1"), { caps: {}, reply: async () => fx("srv2") }); f = a.frame();
  await f.click(".pages .pg:has-text('التالي')"); await a.page.waitForTimeout(300);
  check("host without tool proxy: clear message, no crash", (await txt(f, ".errorbox")).includes("لا يسمح") && (await a.calls()).length === 0); await a.page.close();
  const s = await open(fx("stacked"), { width: 900 }); f = s.frame();
  check("stacked_bar renders one rect per series per category", (await f.$$("svg rect")).length === 6);
  await f.hover("svg rect >> nth=0"); check("tooltip shows category and exact value", (await txt(f, ".charttip")).includes("100 YER")); await s.page.close();
  const g = await open(fx("gl")); f = g.frame();
  check("GL grouped by account with subtotals", (await f.$$("tr.kind-group")).length === 3); await g.page.close();
  const m = await open(fx("wide"), { width: 390 }); f = m.frame();
  check("mobile: cards shown, table hidden, no horizontal overflow", (await f.$$eval(".rowcard", r => r.length)) > 0 && await f.evaluate(() => getComputedStyle(document.querySelector(".tablewrap")).display === "none" && document.documentElement.scrollWidth <= document.documentElement.clientWidth)); await m.shot("i_mobile"); await m.page.close();
  const e = await open(fx("empty")); f = e.frame();
  check("empty result offers a follow-up when the host can message", (await txt(f, ".empty button")) === "اسأل المساعد");
  await f.click(".empty button"); check("follow-up goes through ui/message", (await e.reqs()).some(r => r.method === "ui/message")); await e.page.close();
}
// 9b. view lifecycle telemetry: one batched app-only call through the standard bridge, in order, carrying no data
{
  const t = await open(fx("s2_approval"), { width: 760 }); await t.page.waitForTimeout(900);
  const calls = await t.events();
  const ev = calls.flatMap(c => (c.events || []).map(e => e.event));
  check("view reports handshake -> result -> render, in order, in one call", calls.length === 1 && ev.join() === "ui_handshake_started,ui_handshake_completed,ui_tool_result_received,ui_render_started,ui_render_completed", calls.length + " / " + ev.join());
  check("telemetry carries only event names and a short label", calls.every(c => Object.keys(c).every(k => ["events", "view"].includes(k)) && c.events.every(e => Object.keys(e).every(k => ["event", "detail"].includes(k)) && String(e.detail || "").length < 60)));
  await t.page.close();
  const n = await open(fx("s2_approval"), { width: 760, caps: {} }); await n.page.waitForTimeout(900);
  check("host without tool proxy: view renders and sends no telemetry", (await n.events()).length === 0 && (await txt(n.frame(), "h1")).includes("فاتورة بيع")); await n.page.close();
}
// 10. bulk approval: documents as readable blocks, one approval for all
{
  const t = await open(fx("s7_bulk"), { width: 760, reply: async () => fx("approval_done") }); const f = t.frame();
  check("bulk: one block per document with its type, name and facts", (await f.$$(".docblock")).length === 2 && (await txt(f, ".docblock .dochead b")) === "حمود صالح" && (await f.$$(".docblock .fact")).length >= 4);
  check("bulk: subtitle is the assistant's summary, status pill says approval needed", (await txt(f, ".ag-sub")).includes("فاتورة بيع نقدية") && (await txt(f, ".ag-head .badge")) === "تحتاج موافقة");
  check("bulk: reject and one approve (no per-document edit)", !(await f.$(".ag-foot .btn:has-text('تعديل')")) && (await txt(f, ".btn.success")).includes("موافقة وإنشاء المستندات"));
  await f.click(".btn.success"); await t.page.waitForTimeout(300);
  check("bulk: approve sends a single approvals__decide", (await t.calls()).length === 1 && (await t.calls())[0].name === "approvals__decide");
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.shot("i_bulk"); await t.page.close();
}
// 11. a host that renders through its own bridge (no standard tool-result): guarded fallback shows the same card
{
  const fixture = fx("s2_approval");
  const t = await open(fixture, { width: 760, sendResult: false, init: async fr => { await fr.evaluate(r => { window.openai = { toolOutput: r.structuredContent, toolResponseMetadata: r._meta }; }, fixture); } }); const f = t.frame();
  await t.page.waitForTimeout(1900);
  check("vendor bridge: card appears from the host's own result object when the standard result never arrives", (await txt(f, "h1")).includes("فاتورة بيع") && !!(await f.$(".product")));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
  const n = await open(fixture, { width: 760, sendResult: false }); const g = n.frame(); await n.page.waitForTimeout(1900);
  check("no vendor object and no result: nothing invented, still the loading skeleton", (await g.$$(".skeleton")).length > 0 && !(await g.$(".product"))); await n.page.close();
}
// 12. a host that hands the view structuredContent only (no _meta): the view loads its full model itself
{
  const strip = r => { const c = JSON.parse(JSON.stringify(r)); delete c._meta; return c; };            // what such a host delivers
  const full = r => { const c = JSON.parse(JSON.stringify(r)); c.structuredContent.presentation = Object.assign({}, c._meta["ai_agent/presentation"], { full: true }); delete c._meta; return c; };   // what view__load returns
  const inv = fx("wide");
  const t = await open(strip(inv), { width: 1000, reply: async p => p.name === "view__load" ? full(inv) : { isError: true, content: [{ type: "text", text: "unexpected " + p.name }] } }); let f = t.frame();
  await t.page.waitForTimeout(400);
  const c = await t.calls();
  check("structuredContent only: the view calls view__load with the ref", c.length === 1 && c[0].name === "view__load" && c[0].arguments.ref === inv.structuredContent.presentation.ref, JSON.stringify(c[0]).slice(0, 200));
  check("structuredContent only: full table with all rows is rendered", (await f.$$eval("tbody tr:not(.detail)", r => r.length)) === 9 && (await txt(f, "h1")) === "فواتير البيع");
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();

  const apr = fx("s2_approval");
  const a = await open(strip(apr), { width: 760, reply: async p => p.name === "view__load" ? full(apr) : strip(full(fx("approval_done"))) }); f = a.frame();
  await a.page.waitForTimeout(400);
  check("structuredContent only: approval card shows its working buttons", (await f.$$(".ag-foot .btn")).length >= 3 && !!(await f.$(".btn.success")));
  await f.click(".btn.success"); await a.page.waitForTimeout(400);
  const ac = await a.calls();
  check("structuredContent only: approve button calls approvals__decide with id, decision and token", ac[1]?.name === "approvals__decide" && ac[1].arguments.decision === "approve" && !!ac[1].arguments.approval_id && !!ac[1].arguments.token, JSON.stringify(ac[1]));
  check("structuredContent only: success state with the document link", (await txt(f, ".ag-head .badge")).includes("تحقق") && (await txt(f, ".outcome")).includes("ACC-SINV-2026-777"));
  check("no JS errors", a.errs.length === 0, a.errs.join("|")); await a.shot("i_sc_only_approval"); await a.page.close();

  const n = await open(strip(inv), { width: 1000, caps: {} }); f = n.frame(); await n.page.waitForTimeout(400);
  check("structuredContent only and no tool proxy: the server's text is shown, never an empty table", !(await f.$(".empty")) && !(await f.$("table")) && (await txt(f, "pre")).includes("فواتير البيع")); await n.page.close();
  const e = await open(strip(inv), { width: 1000, reply: async () => ({ isError: true, content: [{ type: "text", text: "{\"error\":\"مرجع العرض غير صالح.\"}" }] }) }); f = e.frame(); await e.page.waitForTimeout(400);
  check("view__load fails: text fallback, no crash", !(await f.$("table")) && (await txt(f, "pre")).length > 20 && e.errs.length === 0); await e.page.close();
}
// 13. redesigned components on the new fixtures
{
  let t = await open(fx("u_approval"), { width: 760 }); let f = t.frame();
  check("approval: financial impact in both currencies, stock impact stated, next steps listed", (await txt(f, ".ag-blocks")).includes("10,600,000 YER") && (await txt(f, ".ag-blocks")).includes("لا يتأثر") && (await f.$$(".nextsteps li")).length === 3);
  check("approval: update_stock = 0 shortage is information only and approve stays available", !!(await f.$(".note.info")) && !(await f.$(".note.critical")) && !!(await f.$(".btn.success")));
  await f.click(".ag-foot .btn:has-text('عرض التفاصيل')");
  check("approval: details open in place with sections and the items table", (await f.$$(".ag-blocks.details .ag-block")).length >= 2 && !!(await f.$(".ag-blocks.details table")));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
  for (const [name, label] of [["u_approval_failed", "فشل التنفيذ"], ["u_approval_expired", "انتهت الصلاحية"], ["u_approval_rejected", "مرفوض"], ["u_approval_verified", "نُفّذ وتم التحقق"]]) {
    t = await open(fx(name), { width: 760 }); f = t.frame();
    check(`state ${name.slice(11)}: badge, outcome message, no decision buttons`, (await txt(f, ".ag-head .badge")) === label && !!(await f.$(".outcome")) && !(await f.$(".btn.success")) && t.errs.length === 0, await txt(f, ".ag-head .badge"));
    await t.page.close();
  }
  t = await open(fx("u_document"), { width: 760 }); f = t.frame();
  const titles = await f.$$eval(".ag-block-title", e => e.map(x => x.textContent.trim()));
  check("document: business sections in order, details collapsed", titles.slice(0, 3).join() === "المعلومات الأساسية,الطرف,المبالغ" && await f.$eval(".ag-block-body", e => e.hidden), titles.join());
  check("document: company-currency total and exchange rate read left to right", (await f.$$eval(".dl .num", e => e.map(x => x.textContent))).includes("1 USD = 530 YER") && (await txt(f, ".ag-blocks")).includes("10,600,000 YER"));
  await f.click(".ag-more"); check("document: 'عرض المزيد' opens the secondary fields", !(await f.$eval(".ag-block-body", e => e.hidden)) && (await f.$eval(".ag-more", e => e.getAttribute("aria-expanded"))) === "true");
  await f.click("td.expander button"); check("document: item row expands to its secondary columns", (await txt(f, "tr.detail")).includes("الكمية بوحدة المخزون"));
  check("no JS errors", t.errs.length === 0, t.errs.join("|")); await t.page.close();
  t = await open(fx("u_trial"), { width: 900, reply: async p => fx("gl") }); f = t.frame();
  check("trial balance: one row per account, findings with a ledger button", (await f.$$("tbody tr")).length === 4 && (await f.$$(".finding")).length === 2 && !(await f.$("tr.kind-group")));
  await f.click(".finding .btn"); await t.page.waitForTimeout(300);
  const call = (await t.calls())[0];
  check("finding -> general ledger of that account, same period, without the model", call?.name === "reports__run" && call.arguments.report_name === "General Ledger" && call.arguments.filters.account.length === 1 && call.arguments.filters.from_date === "2026-01-01", JSON.stringify(call));
  check("drill keeps one product: back button returns to the trial balance", !!(await f.$(".ag-back")));
  await f.click(".ag-back"); await t.page.waitForTimeout(200); check("back restores the trial balance", (await txt(f, "h1")) === "ميزان المراجعة"); await t.page.close();
  t = await open(fx("u_error"), { width: 600 }); f = t.frame();
  check("error: title, reason and next step for a permission problem, no raw tool text, no retry", (await txt(f, ".errorbox-head")).includes("لا تملك الصلاحية") && !(await txt(f, ".errorbox")).includes("No read permission") && !(await f.$(".errorbox button")));
  await t.page.close();
  t = await open(fx("u_error_retry"), { width: 600 }); f = t.frame();
  check("error: a recoverable problem offers retry through the conversation", !!(await f.$(".errorbox button")) && (await txt(f, ".errorbox-head")).includes("السنة المالية"));
  await f.click(".errorbox button"); check("retry asks the assistant (ui/message)", (await t.reqs()).some(r => r.method === "ui/message")); await t.page.close();
  // a host context change must not rebuild the view: what the user sorted stays sorted
  t = await open(fx("wide"), { width: 1000 }); f = t.frame();
  await f.click("th:has-text('الإجمالي') button");
  const before = await f.$eval("th[aria-sort=ascending]", e => e.textContent.trim());
  await t.page.evaluate(() => document.getElementById("f").contentWindow.postMessage({ jsonrpc: "2.0", method: "ui/notifications/host-context-changed", params: { theme: "dark", containerDimensions: { maxHeight: 700 } } }, "*"));
  await t.page.waitForTimeout(300);
  check("host context change keeps the user's sort and applies the theme", !!(await f.$("th[aria-sort=ascending]")) && before.includes("الإجمالي") && (await f.evaluate(() => document.documentElement.dataset.theme)) === "dark");
  await t.page.close();
}
console.log(results.every(Boolean) ? `ALL ${results.length} PASSED` : `${results.filter(x => !x).length} FAILED of ${results.length}`);
await browser.close();
