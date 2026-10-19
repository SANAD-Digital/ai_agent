// Customization approval cards: every case in Arabic (rtl) and English (ltr), light and dark, desktop and mobile.
// Fixtures: dump_fixtures_d.py.txt. Usage: node customization.mjs <dir>   (<dir>/fx holds main_<lang>.html and cz_*.json)
import { chromium } from "playwright"; import fs from "fs";
const DIR = process.argv[2];
const fx = n => JSON.parse(fs.readFileSync(`${DIR}/fx/${n}.json`, "utf8"));
const view = lang => fs.readFileSync(`${DIR}/fx/main_${lang}.html`, "utf8");
const caps = { serverTools: {}, openLinks: {}, downloadFile: {}, updateModelContext: { text: {} }, message: { text: {} } };
const hostPage = (html, fixture, width, theme, lang) => `<!doctype html><body style="margin:0;background:${theme === "dark" ? "#1b1b19" : "#fff"}"><iframe id="f" style="width:${width}px;border:0" scrolling="no"></iframe><script>
const fixture=${JSON.stringify(fixture)}; const f=document.getElementById("f"); f.srcdoc=${JSON.stringify(html).replace(/<\//g, "<\\/")};
window.addEventListener("message", e=>{const m=e.data; if(!m||m.jsonrpc!=="2.0")return; const reply=r=>f.contentWindow.postMessage({jsonrpc:"2.0",id:m.id,result:r},"*");
 if(m.method==="ui/initialize")reply({protocolVersion:"2026-01-26",hostInfo:{name:"m",version:"1"},hostCapabilities:${JSON.stringify(caps)},hostContext:{theme:"${theme}",locale:"${lang === "ar" ? "ar-YE" : "en-US"}",platform:${width < 600 ? '"mobile"' : '"web"'},availableDisplayModes:["inline","fullscreen"],containerDimensions:{width:${width},maxHeight:3000}}});
 else if(m.method==="ui/notifications/initialized")f.contentWindow.postMessage({jsonrpc:"2.0",method:"ui/notifications/tool-result",params:fixture},"*");
 else if(m.method==="ui/notifications/size-changed"&&m.params.height)f.style.height=m.params.height+"px"; else if(m.id!=null)reply({});});
</script></body>`;
const CASES = ["server_script", "client_script_update", "properties", "custom_field", "report", "executed"];
const runs = [];
for (const lang of ["ar", "en"]) for (const c of CASES) runs.push([c, lang, 760, "light"]);
for (const lang of ["ar", "en"]) runs.push(["server_script", lang, 760, "dark"], ["client_script_update", lang, 760, "dark"]);
runs.push(["server_script", "ar", 390, "light"], ["report", "en", 390, "dark"]);
const b = await chromium.launch(); const errs = []; const checks = [];
for (const [name, lang, width, theme] of runs) {
  const shot = `cz_${name}_${lang}_${theme}${width < 600 ? "_m" : ""}`;
  const page = await b.newPage({ viewport: { width, height: 900 }, colorScheme: theme });
  page.on("pageerror", e => errs.push(shot + ": " + e.message)); page.on("console", m => m.type() === "error" && errs.push(shot + " console: " + m.text()));
  await page.setContent(hostPage(view(lang), fx(`cz_${name}_${lang}`), width, theme, lang)); await page.waitForTimeout(700);
  const frame = page.frames()[1];
  const info = await frame.evaluate(() => ({
    dir: document.documentElement.dir, approve: !!Array.from(document.querySelectorAll(".ag-foot button")).find(b => /approve|موافق|اعتماد/i.test(b.textContent) && !/ERP/.test(b.textContent)),
    codeLines: document.querySelectorAll(".cz-src .cz-line").length, checks: document.querySelectorAll(".cz-check").length,
    codeDir: (document.querySelector(".cz-code") || {}).dir || null, overflowX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  }));
  if (name === "server_script" && width > 600) { // the finding's line button jumps to the line in the code
    await frame.click(".cz-lineref"); await page.waitForTimeout(150);
    info.flashed = await frame.evaluate(() => !!document.querySelector(".cz-line.flash[data-line='6']"));
  }
  checks.push([shot, info]);
  await page.screenshot({ path: `${DIR}/${shot}.png`, fullPage: true }); await page.close();
}
for (const [shot, info] of checks) console.log(shot, JSON.stringify(info));
console.log("ERRORS:", errs.length ? errs : "none"); await b.close();
