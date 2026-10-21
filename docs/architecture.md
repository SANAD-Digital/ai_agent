# ai_agent — architecture (2026-10-22)

Musaed is an ERP assistant inside Frappe/ERPNext v16. It has four parts. All writes pass through the same approval engine.

## 1. Chat engine (`engine/`)
- **`runner.py`** runs one assistant message as a background job (`execute_message`).
  - Streams blocks into `AI Message Block` and publishes realtime events to the owner only.
  - Handles stop, the fallback model, and compaction.
- **`loop.py`** is the model ↔ tools loop.
  - Covers steps, tool budgets, the repeated-error guard, the max-tokens handling, and one nudge when tools ran but no answer was written.
- **`providers/`** holds the providers: Anthropic (native streaming, adaptive thinking with summarized display) and OpenAI-compatible. `factory.provider_for()` picks one from **AI Provider**.
- **`tools.py`** is the `ToolExecutor`.
  - Wraps every tool result as untrusted data and masks sensitive fields.
  - Turns `actions.*` / `customize.*` calls into approval requests.
- **`context/`** builds the system prompt (with the untrusted-data rule), history, compaction, and files.
- The Desk UI is `public/js/chat/*` (plain JS, no build step). The API is `chat/api.py`. The support bubble is `chat/support.py`.

## 2. MCP server (`mcp/`)
- **`api.handle`** is the MCP endpoint for Claude, ChatGPT and other hosts.
  - OAuth tokens of AI connectors are confined to it (`token_scope.py`).
- **`service.py`** exposes the tools and creates one `AI Run` per tool call.
- The MCP App view is `mcp/ui/src/*`. It is rendered by the host and in the Desk chat (`viewhost.js`).
- **Approval from MCP:** only the card's signed Approve button, or ERPNext. Text in the chat never approves.

## 3. Runtime and tools (`runtime/`, `tools/`, `query/`)
- **`tool_gateway.py`** checks agent, skill and argument rules, then runs the tool handler.
  - Writes one `AI Tool Call` audit row per call (Executed, Failed or Denied).
- **`approval_engine.py`** handles request → approve → execute → verify.
  - Covers four-eyes, expiry, payload hash, unchanged-document checks, and the MCP retry de-duplication.
- **`policy.py`** decides what can never be written: the code, access and agent-configuration DocTypes, and the provider policy.
- **`masking.py`** hides salaries, IBANs and IDs. **`query/validator.py`** keeps protected fields from being renamed, aggregated or filtered.

## 4. Customization (`customization/`)
- **`kinds/*`** covers fields, properties, client and server scripts, reports, print formats, notifications, workflows, workspaces, dashboards, list settings, naming, permissions, web pages and forms, and custom DocTypes.
  - Each kind describes itself in `kinds/<kind>_tool.json`.
- **`analyzers/*`** are the static checks that run before a human approves:
  - Python: `safe_exec` rules, aliases, computed DocTypes.
  - JavaScript: acorn.
  - SQL: sqlglot.
  - Jinja.
- They are owned by the **Implementation** agent. Each kind has an AI Settings switch, and the Frappe roles of the kind apply.

## Operations
- **AI Agent Health report and hourly alert:** `runtime/health.py`.
- **Retention through Log Settings:** `AI Tool Call` after 90 days. `AI Run` after 180 days, only for runs outside a conversation.
- **Personal data deletion:** `user_data_fields` in `hooks.py`.

## Removed
- WhatsApp support, together with the old orchestrator agent loop (`runtime/orchestrator.py` and its helpers), was removed on 2026-10-22.
- `AI Run Step` is no longer written. Its DocType goes once every site has migrated past this change.
