import { chromium } from "playwright";
import fs from "fs";
const flow = JSON.parse(fs.readFileSync(process.argv[2] + "/flow.json", "utf8")).apps;
const steps = [];
// The mock host uses ONLY wire payloads produced by the real dispatcher.
const uri = flow.tool._meta.ui.resourceUri;
const listed = flow.resources_list.resources.find(r => r.uri === uri);
const read = flow.resources_read.contents.find(c => c.uri === uri);
steps.push(["tool _meta.ui.resourceUri", uri], ["resources/list mimeType", listed.mimeType], ["resources/read mimeType", read.mimeType], ["structuredContent", JSON.stringify(flow.tools_call.structuredContent)]);
const hostPage = `<!doctype html><body><iframe id="f" style="width:420px;border:0"></iframe><script>
const read = ${JSON.stringify(read.text).replace(/<\//g, "<\\/")}, result = ${JSON.stringify(flow.tools_call)};
const log = window.__log = []; const f = document.getElementById("f"); f.srcdoc = read;
const reply = (m, r) => f.contentWindow.postMessage({jsonrpc:"2.0", id:m.id, result:r}, "*");
window.addEventListener("message", e => { const m = e.data; if (!m || m.jsonrpc !== "2.0") return; log.push(m.method);
  if (m.method === "ui/initialize") { window.__init = m.params; reply(m, {protocolVersion:"2026-01-26", hostInfo:{name:"mock-host",version:"1.0"}, hostCapabilities:{}, hostContext:{theme:"dark", locale:"ar-YE", timeZone:"Asia/Aden", platform:"web"}}); }
  else if (m.method === "ui/notifications/initialized") { const w = f.contentWindow;
    w.postMessage({jsonrpc:"2.0", method:"ui/notifications/tool-input", params:{arguments:{}}}, "*");
    w.postMessage({jsonrpc:"2.0", method:"ui/notifications/tool-result", params:result}, "*");
    w.postMessage({jsonrpc:"2.0", method:"ui/notifications/host-context-changed", params:{theme:"light"}}, "*"); }
  else if (m.method === "ui/notifications/size-changed" && m.params.height) f.style.height = m.params.height + "px"; });
</script></body>`;
const browser = await chromium.launch(); const page = await browser.newPage({ viewport: { width: 460, height: 400 } });
const errors = []; page.on("pageerror", e => errors.push(e.message)); page.on("console", m => m.type() === "error" && errors.push(m.text()));
await page.setContent(hostPage); await page.waitForTimeout(600);
const frame = page.frames().find(x => x !== page.mainFrame());
const dom = await frame.evaluate(() => ({ status: document.getElementById("status").textContent, count: document.getElementById("count").textContent,
  dt: [...document.querySelectorAll("dt")].map((d, i) => d.textContent + "=" + document.querySelectorAll("dd")[i].textContent), dir: document.documentElement.dir, theme: document.documentElement.dataset.theme }));
const order = (await page.evaluate(() => window.__log)).filter((v, i, a) => a.indexOf(v) === i);
const init = await page.evaluate(() => window.__init);
await page.screenshot({ path: process.argv[2] + "/minimal.png" });
console.log(JSON.stringify({ steps, handshake_order: order, app_sent_protocol: init.protocolVersion, dom, errors }, null, 1));
await browser.close();
// fallback: host without MCP Apps
const plain = JSON.parse(fs.readFileSync(process.argv[2] + "/flow.json", "utf8")).plain.tools_call;
console.log("FALLBACK", JSON.stringify({ text: plain.content[0].text, structuredContent: plain.structuredContent, has_meta: "_meta" in plain, isError: plain.isError }));
