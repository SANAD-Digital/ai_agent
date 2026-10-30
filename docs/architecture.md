# ai_agent — architecture (2026-10-22)

Musaed is an ERP assistant inside Frappe/ERPNext v16. It has four parts. All writes pass through the same approval engine.

## 1. Chat engine (`engine/`)
- **`runner.py`** runs one assistant message as a background job (`execute_message`).
  - Handles the loop's events, stop, the fallback model, compaction and the final state of the message.
  - `run_setup.py` wires provider, tools, skills, prompt, memory, history and attachments into the loop.
  - `block_store.py` streams blocks into `AI Message Block`; `publisher.py` sends realtime events to the owner only.
- **`loop.py`** is the model ↔ tools loop.
  - Covers steps, tool budgets, the repeated-error guard, the max-tokens handling, and one nudge when tools ran but no answer was written.
- **`providers/`** holds the providers: Anthropic (native streaming, adaptive thinking with summarized display) and OpenAI-compatible. `factory.provider_for()` picks one from **AI Provider**.
- **`tools.py`** is the `ToolExecutor`.
  - Wraps every tool result as untrusted data and masks sensitive fields.
  - Turns `actions.*` / `customize.*` calls into approval requests.
- **`context/`** builds the system prompt (with the untrusted-data rule), history, compaction, and files.
- The Desk UI is `public/js/chat/*` (plain JS modules), built by `bench build` into `ai_agent_chat.bundle.js` and
  `ai_agent_chat.bundle.css` (prefixed: bundle names are shared by every app on the bench). The page and the support bubble load the bundles on demand (`chat_bubble.js`). The API is `chat/api.py`. The support bubble is `chat/support.py`.

## 2. MCP server (`mcp/`)
- **`api.handle`** is the MCP endpoint for Claude, ChatGPT and other hosts.
  - OAuth tokens of AI connectors are confined to it (`token_scope.py`).
- **`service.py`** exposes the tools and creates one `AI Run` per tool call.
- **`profiles.py`** picks the user's `AI MCP` record for the request (the one for its OAuth client, else the user's
  general one): its agent, company and switch. No record: the AI Settings agent, company and roles.
- The MCP App view is `mcp/ui/src/*`. It is rendered by the host and in the Desk chat (`viewhost.js`).
- **Approval from MCP:** only the card's signed Approve button, or ERPNext. Text in the chat never approves.

## 3. Runtime and tools (`runtime/`, `tools/`, `query/`)
- **`tool_gateway.py`** checks agent, skill and argument rules, then runs the tool handler.
  - Writes one `AI Tool Call` audit row per call (Executed, Failed or Denied).
- **`approval_engine.py`** handles request → approve → execute → verify. `decide()` is the only decision path.
  - `approval_checks.py`: who may request or execute; `approval_execution.py`: one executor per action type.
  - Covers four-eyes, expiry, payload hash, unchanged-document checks, and the MCP retry de-duplication.
- **`policy.py`** decides what can never be written: the code, access and agent-configuration DocTypes, and the provider policy.
- **`masking.py`** hides salaries, IBANs and IDs, and holds the one list of credential fields (`is_secret_field`). **`query/validator.py`** keeps protected fields from being renamed, aggregated or filtered.
- **Tool names:** `data.query` in the registry and `AI Tool Call`; `data__query` at providers and MCP hosts and in `AI Message Block`. Convert only through `runtime/tool_names.py`.

## 4. Customization (`customization/`)
- **`kinds/*`** covers fields, properties, client and server scripts, reports, print formats, notifications, workflows, workspaces, dashboards, list settings, naming, permissions, web pages and forms, and custom DocTypes.
  - Each kind describes itself in `kinds/<kind>_tool.json`.
  - Large kinds keep their request data apart: `workspace_spec.py` for workspaces, `analyzers/web_content.py` for web pages.
- **`analyzers/*`** are the static checks that run before a human approves:
  - Python: `safe_exec` rules, aliases, computed DocTypes.
  - JavaScript: acorn 8, shipped in `analyzers/vendor/acorn` (node comes from `site_config.node_path`, then PATH, then nvm).
  - SQL: sqlglot.
  - Jinja.
- They are owned by the **Implementation** agent. Each kind has an AI Settings switch, and the Frappe roles of the kind apply.
- Raw SQL in a kind quotes names only through `kinds/_base.table()` / `column()`, which refuse a table or column that does not exist.

## Install and migrate
- Frappe syncs the app's content from its own places: the workspace, `desktop_icon/`, `workspace_sidebar/`,
  the roles (from the DocType permissions) and the standard agents, plugins and preset providers
  (`ai_agent/ai_agent/{ai_agent,ai_plugin,ai_provider}/`, `importable_doctypes`). A re-import keeps what the site
  owns (`standard_records.py`).
- `install.sync` (after install and migrate) handles only what comes from code: tool options, skills and their
  grants, the support chatbot agent and the default metrics. Details: `STANDARD_CONFIGURATION.md`.

## Operations
- **CI** (`.github/workflows/ci.yml`, on every push to main and every pull request): `ruff` (undefined names,
  unused imports, syntax), then the full test suite on a fresh bench with Frappe and ERPNext 16;
  the fresh site gets ERPNext's test data first (`erpnext.tests.bootstrap_test_data`, as ERPNext's own CI does).
- **AI Agent Health report and hourly alert:** `runtime/health.py`.
- **Retention through Log Settings:** `AI Tool Call` after 90 days. `AI Run` after 180 days, only for runs outside a conversation.
- **Personal data deletion:** `user_data_fields` in `hooks.py`.

## Removed
- WhatsApp support, together with the old orchestrator agent loop (`runtime/orchestrator.py` and its helpers), was removed on 2026-10-22.
- `AI Run Step`, its links and the step plumbing (gateway `step_name`, the interceptor's step hooks) were removed
  on 2026-10-22, in two changes with a migrate between them: while a site's AI Run still lists the child table,
  every new run needs its controller. The old rows stay in their table.
- The connection fields of AI Settings from before AI Provider (`provider_type`, `base_url`, `api_key`,
  `default_model`, `strict_tool_calling`) were removed the same day.
