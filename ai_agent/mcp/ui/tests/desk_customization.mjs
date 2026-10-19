// The Desk chat approval block for customization requests, rendered by the real chat.css + core.js + blocks.js
// with the snapshots from dump_fixtures_d.py.txt. Usage: node desk_customization.mjs <dir> <apps/ai_agent/ai_agent/public>
import { chromium } from "playwright"; import fs from "fs";
const [DIR, PUB] = process.argv.slice(2);
const read = p => fs.readFileSync(p, "utf8");
const css = ["musaed-brand/tokens/tokens.css", "css/chat.css"].filter(p => fs.existsSync(`${PUB}/${p}`)).map(p => read(`${PUB}/${p}`)).join("\n");
const js = ["js/chat/core.js", "js/chat/blocks.js"].map(p => read(`${PUB}/${p}`)).join("\n;\n");
const page = (snap, lang, theme) => `<!doctype html><html dir="${lang === "ar" ? "rtl" : "ltr"}" lang="${lang}" data-theme="${theme}"><head><meta charset="utf-8">
<style>${css}</style><style>body{margin:0;padding:16px;background:var(--ac-bg,#fff);font-family:"IBM Plex Sans Arabic","Noto Sans Arabic",system-ui,sans-serif;color:var(--ac-text)} #m{max-width:720px;margin:auto}</style></head>
<body class="ac-root"><div id="m"></div><script>window.frappe={};window.__=(t,a)=>a?t.replace(/\\{(\\d+)\\}/g,(m,i)=>a[i]):t;</script><script>${js.replace(/<\/script>/g, "<\\/script>")}</script>
<script>const snap=${JSON.stringify(snap)}; window.SENT=[];
const r=AIChat.blocks.render({kind:"approval",seq:5,status:snap.status==="Pending"?"streaming":"completed",data:snap},{onSend:t=>window.SENT.push(t)}); document.getElementById("m").appendChild(r.el);</script></body></html>`;
const CASES = ["server_script", "client_script_update", "properties", "custom_field", "report", "executed"];
const runs = [];
for (const lang of ["ar", "en"]) for (const c of CASES) runs.push([c, lang, "light"]);
runs.push(["server_script", "ar", "dark"], ["client_script_update", "en", "dark"], ["executed", "ar", "dark"], ["report", "ar", "light", 390]);
const b = await chromium.launch(); const errs = [];
for (const [name, lang, theme, width = 800] of runs) {
  const shot = `desk_${name}_${lang}_${theme}${width < 600 ? "_m" : ""}`;
  const p = await b.newPage({ viewport: { width, height: 900 }, colorScheme: theme });
  p.on("pageerror", e => errs.push(shot + ": " + e.message));
  await p.setContent(page(JSON.parse(read(`${DIR}/fx/desk_${name}_${lang}.json`)), lang, theme)); await p.waitForTimeout(300);
  const info = await p.evaluate(() => {
    const btns = Array.from(document.querySelectorAll(".ac-approval-actions button")).filter(b => !b.hidden).map(b => b.textContent.trim());
    return { buttons: btns, lines: document.querySelectorAll(".ac-cz-src .ac-cz-line").length, overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth };
  });
  if (name === "executed") { const u = await p.$(".ac-cz-outcome button"); if (u) { await u.click(); info.sent = await p.evaluate(() => window.SENT); } }
  console.log(shot, JSON.stringify(info));
  await p.screenshot({ path: `${DIR}/${shot}.png`, fullPage: true }); await p.close();
}
console.log("ERRORS:", errs.length ? errs : "none"); await b.close();
