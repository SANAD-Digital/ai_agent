// Our server's real wire results validated with the official spec schemas of @modelcontextprotocol/client (2026-07-28).
import fs from "fs";
import * as sdk from "@modelcontextprotocol/client";
const wire = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const schemas = sdk.specTypeSchemas;
const names = Object.keys(schemas);
if (process.argv[3] === "list") { console.log(names.filter(n => /Result$/.test(n)).join(" ")); process.exit(0); }
const map = { "server/discover": "DiscoverResult", "tools/list": "ListToolsResult", "resources/list": "ListResourcesResult", "resources/templates/list": "ListResourceTemplatesResult", "resources/read": "ReadResourceResult", "tools/call": "CallToolResult" };
let ok = 0, bad = 0;
for (const [key, response] of Object.entries(wire)) {
  const schema = schemas[map[key.split(" ")[0]]];
  const result = response.result;
  let verdict;
  try {
    const out = schema["~standard"] ? await schema["~standard"].validate(result) : schema.safeParse(result);
    const issues = out.issues || (out.success === false ? out.error.issues : null);
    verdict = issues ? "INVALID " + JSON.stringify(issues).slice(0, 300) : "valid";
  } catch (e) { verdict = "ERROR " + e.message; }
  verdict === "valid" ? ok++ : bad++;
  console.log(verdict === "valid" ? "PASS" : "FAIL", key, "->", map[key.split(" ")[0]], verdict === "valid" ? "" : verdict);
}
// Required-ness of ttlMs / cacheScope is enforced by the SDK's internal 2026 wire codec (not exported); checked here directly.
const cacheable = Object.entries(wire).filter(([k]) => /^(server\/discover|tools\/list|resources\/)/.test(k));
const rejected = cacheable.every(([, r]) => Number.isInteger(r.result.ttlMs) && r.result.ttlMs >= 0 && ["public", "private"].includes(r.result.cacheScope) && r.result.resultType === "complete");
console.log(rejected ? "PASS" : "FAIL", "every cacheable result carries ttlMs, cacheScope and resultType");
console.log(bad === 0 && rejected ? `ALL ${ok + 1} PASSED` : `${bad + (rejected ? 0 : 1)} FAILED`);
