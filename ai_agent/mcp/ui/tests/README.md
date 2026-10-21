# Browser tests for the MCP App view

These drive the real assembled view (`protocol.build_main_view()`) inside an iframe served by a mock MCP Apps
host that speaks the standard `ui/*` protocol, using fixtures produced by the real presenters and service.
They are not part of `bench run-tests` (they need Playwright + Chromium, kept outside the repo).

1. Fixtures: copy `dump_fixtures_a.py.txt` and `dump_fixtures_b.py.txt` to `ai_agent/tests/test_zz_dump_a.py` / `_b.py`, run
   `FIXTURE_DIR=<dir>/fx bench --site <site> run-tests --app ai_agent --module ai_agent.tests.test_zz_dump_a` (and `_b`), then delete the copies.
   They also write `fx/main.html` (the assembled view).
2. `cd <folder with playwright installed> && node <repo>/ai_agent/mcp/ui/tests/interact.mjs <dir>` runs ~70 checks
   (server paging/sort/filters, tabs, approval, blocked stock, choice, capabilities fallback, mobile, focus).
3. `scenarios.mjs <dir>` renders the six reference scenarios (desktop, mobile, dark) to PNGs.
4. Customization cards: `dump_fixtures_d.py.txt` (same procedure, module `test_zz_dump_d`) writes `fx/main_ar.html`,
   `fx/main_en.html`, `fx/cz_*.json` and `fx/desk_*.json` from the cases in `tests/test_customization_cards.py`.
   `customization.mjs <dir>` renders every case in the MCP view (Arabic rtl and English ltr, light, dark, mobile) and
   checks there is no approve button, the code is complete and a finding's line button jumps to the line;
   `desk_customization.mjs <dir> <repo>/ai_agent/public` renders the same cases as Desk chat approval blocks.

## Reference host (official MCP Apps bridge)

`reference_host.mjs` runs both views against the official `AppBridge` from `@modelcontextprotocol/ext-apps` (with its
schema validation), in a sandboxed iframe under a no-network CSP, including a host that starts listening late.
Setup outside the repo: `mkdir refhost && cd refhost && npm i @modelcontextprotocol/ext-apps esbuild`, copy
`reference_host_entry.js` to `refhost/host-entry.js`, then
`node_modules/.bin/esbuild host-entry.js --bundle --format=iife --platform=browser --outfile=host-bundle.js`
and `node reference_host.mjs <dir>` (`<dir>` holds `refhost/` and `fx/`).
