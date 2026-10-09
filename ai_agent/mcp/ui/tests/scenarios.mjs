import { chromium } from "playwright"; import fs from "fs";
const DIR = process.argv[2]; const VIEW = fs.readFileSync(`${DIR}/fx/main.html`, "utf8");
const fx = n => JSON.parse(fs.readFileSync(`${DIR}/fx/${n}.json`, "utf8"));
const caps = { serverTools: {}, openLinks: {}, downloadFile: {}, updateModelContext: { text: {} }, message: { text: {} } };
const hostPage = (fixture, width, theme) => `<!doctype html><body style="margin:0;background:${theme === "dark" ? "#0b1018" : "#fff"}"><iframe id="f" style="width:${width}px;border:0" scrolling="no"></iframe><script>
const fixture=${JSON.stringify(fixture)}; const f=document.getElementById("f"); f.srcdoc=${JSON.stringify(VIEW).replace(/<\//g, "<\\/")};
window.addEventListener("message", e=>{const m=e.data; if(!m||m.jsonrpc!=="2.0")return; const reply=r=>f.contentWindow.postMessage({jsonrpc:"2.0",id:m.id,result:r},"*");
 if(m.method==="ui/initialize")reply({protocolVersion:"2026-01-26",hostInfo:{name:"m",version:"1"},hostCapabilities:${JSON.stringify(caps)},hostContext:{theme:"${theme || "light"}",locale:"ar-YE",platform:${width < 600 ? '"mobile"' : '"web"'},availableDisplayModes:["inline","fullscreen"],containerDimensions:{width:${width},maxHeight:1200}}});
 else if(m.method==="ui/notifications/initialized")f.contentWindow.postMessage({jsonrpc:"2.0",method:"ui/notifications/tool-result",params:fixture},"*");
 else if(m.method==="ui/notifications/size-changed"&&m.params.height)f.style.height=m.params.height+"px"; else if(m.id!=null)reply({});});
</script></body>`;
const b = await chromium.launch(); const errs = [];
const runs = [["s1_companies", 760], ["s2_approval", 760], ["s3_statement", 760], ["s4_sales", 900], ["s5_blocked", 760], ["s6_choice", 760],
  ["s2_approval", 390, "light", "m_approval"], ["s3_statement", 390, "light", "m_statement"], ["s4_sales", 390, "light", "m_sales"], ["s5_blocked", 390, "light", "m_blocked"],
  ["s1_companies", 760, "dark", "d_companies"], ["s3_statement", 760, "dark", "d_statement"]];
for (const [name, width, theme = "light", shot = name] of runs) {
  const page = await b.newPage({ viewport: { width, height: 900 } });
  page.on("pageerror", e => errs.push(shot + ": " + e.message)); page.on("console", m => m.type() === "error" && errs.push(shot + " console: " + m.text()));
  await page.setContent(hostPage(fx(name), width, theme)); await page.waitForTimeout(700);
  await page.screenshot({ path: `${DIR}/${shot}.png`, fullPage: true }); await page.close();
}
console.log("ERRORS:", errs.length ? errs : "none"); await b.close();
