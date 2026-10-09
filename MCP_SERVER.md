# AI Agent MCP Server

The app exposes its existing permission-aware ERP tools through a stateless
MCP Streamable HTTP endpoint:

```text
https://YOUR-SITE/api/method/ai_agent.mcp.api.handle
```

Every request is authenticated by Frappe and runs as the authenticated user.
DocType permissions, User Permissions, agent policy, tool validation, result
limits, audit records, and approval requests remain enforced server-side.
The endpoint supports the current stateless MCP `2026-07-28` protocol and the
legacy handshake protocol through `2025-11-25` for older clients.

## Enable it

1. Run `bench --site YOUR-SITE migrate` after deploying this version.
2. Open **AI Settings > MCP Server**.
3. Enable **MCP Server**.
4. Keep **MCP Agent** set to `ERP Analyst` for read-only access, or select an
   operations agent and separately enable **MCP Action Requests**.
5. Assign the dedicated `AI MCP User` role to permitted users. **Allowed MCP
   Roles** accepts one role per line; the defaults are `AI MCP User` and
   `System Manager`. Existing ERP roles still determine which records each
   permitted user can read or change.
6. Leave **Allowed MCP Browser Origins** empty for server-to-server clients.
   If a trusted browser client sends an `Origin` header, add its exact HTTPS
   origin explicitly.
7. Adjust **MCP Requests per Minute** if needed. The default is 60 per
   authenticated user and is enforced through Redis across web workers.

The endpoint never executes document-changing action tools directly. When
action requests are enabled, they create an `AI Approval Request`, and a human
decides it: with the card's button, by saying so in chat (`approvals__decide`),
or in Frappe. See "Approval lifecycle" below.

## OAuth for ChatGPT and other remote clients

Frappe v16 provides OAuth 2.1-compatible authorization-code + PKCE metadata.
In **OAuth Settings** verify:

- **Show Auth Server Metadata** is enabled.
- **Show Protected Resource Metadata** is enabled.
- **Enable Dynamic Client Registration** is enabled if the client uses DCR.
- **Allowed Public Client Origins** includes the client origin when a public
  PKCE client is dynamically registered (for ChatGPT, `https://chatgpt.com`).
- HTTPS and the site's canonical public hostname are configured correctly.

Then add the endpoint URL shown above as a custom MCP server in the client.
The user will be redirected to Frappe to sign in and consent. The issued
Bearer token maps every MCP call back to that Frappe user.

For clients that accept static headers during development, Frappe API-token
authentication also works:

```text
Authorization: token API_KEY:API_SECRET
```

Do not use a privileged shared token in production. Prefer per-user OAuth.

### Cline (VS Code) and other header-based clients

Cline has no OAuth flow and no MCP Apps view: it authenticates with a static
header and reads the text and `structuredContent` of every result. Generate
API credentials for the Frappe user the assistant should act as (User form →
**API Access** → *Generate Keys*), then add the server to Cline's
`cline_mcp_settings.json` (MCP Servers → Configure):

```json
{
  "mcpServers": {
    "erpnext": {
      "type": "streamableHttp",
      "url": "https://<site>/api/method/ai_agent.mcp.api.handle",
      "headers": { "Authorization": "token API_KEY:API_SECRET" },
      "timeout": 120,
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

The two failure modes are easy to tell apart:

- `PermissionError … is not whitelisted` (HTTP 403) means the request arrived
  as **Guest**: the `headers` block is missing, the header is not named
  `Authorization`, or its value does not start with `token ` (a `Bearer`
  prefix is treated as OAuth and gives 401 instead).
- `AuthenticationError` (HTTP 401) means the header arrived but the key does
  not belong to an **enabled** user or the secret is wrong.

After `initialize`, Streamable HTTP clients open a GET for a server event
stream; this stateless server answers 405 with `Allow: POST`, which the client
SDK treats as "no stream" and continues.

## Optional short `/mcp` URL

The long API method URL is a valid MCP endpoint. If a client requires `/mcp`,
add an exact reverse-proxy rule that internally forwards `/mcp` to:

```text
/api/method/ai_agent.mcp.api.handle
```

Preserve the HTTP method, request body, `Authorization`, `Accept`, and
`MCP-Protocol-Version` headers. Do not redirect, because some clients will not
forward authorization headers across redirects.

## Verify

Use MCP Inspector and select **Streamable HTTP**:

```bash
npx @modelcontextprotocol/inspector
```

Test initialization, `tools/list`, read calls, denied roles, and invalid
inputs. If action requests are enabled, confirm they only create pending
approval records.

The exposed read surface includes dynamic schema discovery, generic queries,
document and child-row reads, reports, skills, memory search, OCR-session text,
and safe attachment metadata. File contents, arbitrary whitelisted-method
calls, and direct document mutations are intentionally not exposed.

## Result views (MCP Apps)

Results are built in three layers: the tool returns normalized business data, a **presenter**
(`ai_agent/presentation/`) turns it into a deterministic view model (business column order, Arabic
labels, formatted money/dates/status, totals, warnings, actions), and one portable **MCP App**
(`ui://ai-agent/main`, `ai_agent/mcp/ui/view.html`) renders it. Nothing is HTML on the wire.

| Wire field | Content |
|---|---|
| `content` | Short summary plus an ordered Markdown fallback (tools-only hosts, terminals) |
| `structuredContent` | `data` (capped preview), `summary`, `presentation` (no rows, no tokens), `actions`, `warnings`, `meta` |
| `_meta["ai_agent/presentation"]` | Full view model with rows, chart series and signed action tokens; not for the model |

Tools with a presenter carry `_meta.ui.resourceUri` (and the deprecated `ui/resourceUri`). Hosts that
declare `extensions["io.modelcontextprotocol/ui"].mimeTypes` containing `text/html;profile=mcp-app`
get `_meta`; everything else gets the text fallback with no loss of data. Tools without a
presenter, and `data__count`, return text only.

App-only tools (`_meta.ui.visibility: ["app"]`, hidden from the model): `view__page` (page, sort,
search of a table, signed token), `approvals__decide`, `approvals__revise`. `approvals__status`
is model-visible so the assistant can verify an outcome.

### Approval from the card

`actions__request_*` create the pending `AI Approval Request` exactly as before, and return an
approval card: prepare, validate, preview, approval, execute, verify. Buttons are enabled only when
**all** hold: action requests are enabled, **AI Settings > Allow Approval from MCP App** is on
(default off), and the host declared MCP Apps support. Each button carries a token signed over the
approval, its payload hash, the user and the single action. It lives only in `_meta`. A decided or
expired request cannot be replayed, and execution re-checks the approver's permissions. The card is a
business confirmation; it does not replace any native approval a host applies to tool calls.
Otherwise the card shows the Frappe link and approval happens in the desk as before.

### Where the view lives

Plain HTML, CSS and JavaScript, with no framework and no build step. `ai_agent/mcp/ui/src/` holds small modules
(`core.js` helpers, `icons.js` inline SVG icons, `host.js` bridge and capability layer, `components.js`, `table.js`,
`chart.js`, `approval.js` (ready, blocked, edit, result, choice), `cards.js`, `app.js`, `styles.css`, `shell.html`).
`resources/read` assembles them into one self-contained document, so the resource needs no network origin.
Edit the modules, not a generated file. Browser checks live in `ai_agent/mcp/ui/tests/`.

The layout (spacing, radius, type scale, components) is defined in `styles.css`. The palette is neutral by default and
follows the host when it sends the standard MCP Apps style variables (`hostContext.styles.variables`): `host.js` puts
them on the root element and every token falls back through them, so the card reads as part of the conversation.
The Desk chat (`public/js/chat/viewhost.js`) sends its own `--ac-*` palette this way.

`Host.*` (in `host.js`) is the only place that talks to the host, always in this order: the MCP Apps
standard capability the host declared, a vendor API only when the standard has no equivalent and the
function exists (`window.openai` is read in exactly one guarded line), then a plain fallback. The UI never
branches on which host it runs in. `ui://ai-agent/minimal` is a connection check (`app__probe`).

### Tools added for the reference scenarios

| Tool | Purpose |
|---|---|
| `analysis__sales` | Period sales: KPI cards, top customers chart and ranking, computed share insight (read only, permission scoped, refuses to add across currencies) |
| `app__choose` | Ask the user to pick one option (`source`: companies / warehouses, or explicit options); the answer returns as their next message (`ui/message`), or as a numbered text list on hosts without MCP Apps |
| `app__probe` | Connection check for the view |

Approval cards: a ready card (facts, item photo inlined by the server, stock chip), a blocked card when posting would
deduct stock that is not there (view stock / other warehouse / edit quantity / cancel), and, when the four-eyes policy
is on, no approve button on your own request.

### Filters, paging and drill-down without the model

Filter definitions (status/currency selects with the full field options, date ranges) come from the
presenter. The filter bar sends simple comparisons (`=`, `!=`, `>=`, `<=`, `like`, `in`, at most 8) through
`view__page`; the query validator still checks every field and value. Row "open" calls
`data__get_document` from the card.

## Approval lifecycle over MCP

```text
request (actions__request_*)  ->  Pending  ->  decision  ->  Approved  ->  executing  ->  Executed (+verified)  |  Failed
                                                   \->  Rejected                         Pending past its TTL  ->  Expired
```

| Tool | Visible to | Purpose |
|---|---|---|
| `approvals__decide` | model + app | Approve or reject. From chat the user's explicit word ("موافق", "اعتمد") is the decision; the card's buttons call the same tool with a signed token. Executes, verifies and returns the document link. |
| `approvals__list_pending` | model + app | What "موافق" refers to: one pending request returns its card, several return a choice card, none says so. |
| `approvals__status` | model + app | Current state and result of one request. |
| `approvals__revise` | app only | Edit quantity / price / company / warehouse of a pending create request. |

Guarantees: the request row is locked (`SELECT ... FOR UPDATE`) while it is decided and executed; a repeated
"approve" returns the recorded outcome and never runs the action again; expiry, payload hash, the four-eyes policy and
the approver's ERPNext permissions are checked by the engine at decision and again at execution; a failed execution
ends as `Failed` with its reason.

**Original goal.** `actions__request_create_draft` and `actions__request_bulk_create` accept `then: {goal, tool, arguments}`.
After the request is approved and executed the server prepares that next step itself (placeholders such as
`"$created.Customer"` become the names of the records just created) and returns the next request's card. A `then` runs once.

**Three separate switches** (AI Settings > MCP):

| Setting | Meaning |
|---|---|
| Enable MCP Action Requests | Write tools exist at all. |
| MCP Actions Require Approval (`mcp_require_approval`, default on, unset = on) | Off: the requester's own requests execute immediately under their ERPNext permissions, are still recorded as AI Approval Requests (`mode: direct`), and no approval card is shown. |
| Allow Approval from MCP App (`mcp_enable_app_approval`) | The card's buttons may decide. Off: approval is typed in chat or done in Frappe. |

## Diagnostics

`logs/ai_agent_mcp.jsonl` (per site, append-only, safe with many workers) holds one JSON line per lifecycle event: `request_received` / `request_failed` for every request a host makes, plus: `host_seen` (what a host declares on
`initialize` / `tools/list` / `resources/*`), `tool_registered`, `tool_called`, `approval_*`, `continuation_*`,
`ui_resource_requested|loaded|failed`, and the view's own `ui_handshake_*`, `ui_tool_result_received`, `ui_render_*`
(sent by the view in one batched app-only call, `view__event`). Identifiers only: never payloads, arguments or tokens.
`ai_agent.mcp.api.diagnostics` (System Manager) returns the last events. A host renders the view if and only if
`ui_resource_loaded` followed by `ui_render_completed` appear for its user.

## Approval lifecycle

```
actions__request_*  ->  Pending  ->  approvals__decide(approve)  ->  Approved -> Executed -> verified
                                 ->  approvals__decide(reject)   ->  Rejected
                                 ->  (30 min)                    ->  Expired          execution error -> Failed
```

| Tool | Visible to | Purpose |
|---|---|---|
| `approvals__decide` | model + app | Approve or reject. From chat (the user typed "موافق") it needs only `approval_id` + `decision`; the card's buttons add a signed token |
| `approvals__list_pending` | model + app | What "موافق" refers to: one pending request returns its card, several return a choice, none says so |
| `approvals__status` | model + app | Current state and result of a request |
| `approvals__revise` | app only | Edit quantity / price / warehouse before approval |

Checks on every decision: the request exists and is visible to the user, it is `Pending`, not expired, its payload hash is
intact, four-eyes and approver-role policy, and the approver's ERPNext permissions again at execution. Decisions take a row
lock and a repeated `approve` returns the recorded outcome, so an action is never executed twice. After execution the
created or changed documents are read back and the verdict is stored in `execution_result` (`verified`, `approved_via`).

**Original goal (`then`).** `actions__request_create_draft` and `actions__request_bulk_create` accept an optional `then`
`{goal, tool, arguments}`. After the request is approved and executed the server prepares that next step itself (same user,
same checks) and returns its card; `$created.<DocType>` placeholders become the names of the records just created.
Only request tools can be chained, and each continuation runs once.

**Three separate settings**

| Setting | Meaning |
|---|---|
| Enable MCP Action Requests | Write tools are offered at all |
| MCP Actions Require Approval (default on; unset = on) | Off: the requester's own requests execute immediately under their ERPNext permissions, still recorded |
| Allow Approval from MCP App | The card's buttons may decide. Off: approval happens in chat or in Frappe |

## Diagnostics

`logs/ai_agent_mcp.log` holds one JSON line per lifecycle event (`tool_called`, `approval_created` ... `approval_verified`,
`ui_resource_requested`, `ui_resource_loaded`, `ui_handshake_completed`, `ui_tool_result_received`, `ui_render_completed`,
`ui_render_failed` ...) with request id, user, tool, approval id, DocType and host. No payloads, arguments or tokens.
`ai_agent.mcp.api.diagnostics` (System Manager) returns the recent events and what each host declared and fetched. The view
reports its own events through the app-only tool `view__event`; a host that never produces `ui_resource_requested` is not
loading the view at all.

## What reaches the view, and view caching

ChatGPT and other MCP Apps hosts are documented to render from the tool result's `structuredContent`; whether `_meta`
also reaches the view differs between hosts. So `structuredContent.presentation` is lean (no rows, no action arguments)
and carries `ref`, a signed reference bound to the user. When the view receives no `_meta` it calls the app-only tool
`view__load {ref}` and gets the complete view model back in `structuredContent` (`presentation.full = true`). A ref
names an approval request or a read-only tool call to repeat; it can never repeat a write. Results of calls made by
the view itself (`view__load`, `view__page`, a card button) always carry the full view model in `structuredContent`.

Hosts cache a view by its URI, so the URI carries the view's content hash: `ui://ai-agent/main/<hash>`. A changed view
gets a new URI automatically; older URIs keep resolving. After deploying a change, refresh the connector in the host
so it re-reads `tools/list`: hosts keep the tool list they fetched when the connector was added.

## Design system and component architecture

One visual language for every tool: a single flat card (hairline border, no shadow, no backdrop), figures as one ruled
strip, fields as label/value lines, one filled button per card. Tokens live in `ui/src/styles.css` (`:root`): spacing
`--sp-1..6`, card inset `--pad`, radius `--radius-sm|--radius|--radius-lg`, surfaces `--surface|--surface-2|--surface-3`,
`--border`, text `--text|--muted|--faint`, `--accent`/`--on-accent` (the filled button), `--link`, chart `--series-1..4`,
and semantic colours with a soft pair each: `--success`, `--warning`, `--danger`, `--info`. Each token is
`var(<standard host variable>, <default>)`; the `--d-*` defaults are defined for light and dark. The view never scrolls
as a page: it reports its height and the host sizes the frame; only a long table scrolls, inside `--max-table-height`.

| Layer | Where | Responsibility |
|---|---|---|
| Mapping | `presentation/presenters/*`, `labels.py`, `money.py`, `errors.py`, `insights.py` | Sections, field order, labels in the user's language, the currency of every amount, de-duplication, warnings, findings, friendly errors |
| Schema | `presentation/schema.py` | `table`, `document`, `approval`, `choice`, `cards`, `error`; `column`, `cell`, `kpi`, `action`, `warning`, `insight` |
| View | `mcp/ui/src/*.js` | `Table`, `Document`, `Approval`, `Choice`, `Cards`, `ErrorView`, `Chart`, plus `Section`, `FieldGrid`, `Kpis`, `Notes`, `Insights`, `ErrorState`, `EmptyState`, `ActionRow`, `Overflow` |

| Component | States |
|---|---|
| Approval card | pending (review), blocked (stock really short), editing, confirming a rejection, executing, verified / executed, failed, rejected, expired |
| Data table | loading, rows, no data, no match (with "clear filters"), partial data (says "N of M"), error with retry |
| Document card | sections, collapsed details, child tables with expandable rows |
| Error card | permission / policy / limit (amber, no retry), validation and failure (red, retry when it can help) |

Rules the mapping layer enforces: no label appears twice in a document; an amount is printed in the currency ERPNext
declares for its field (transaction, company or party-account currency), with the exchange rate shown once; equal
amounts are shown once; unchecked boxes, zero amounts and technical fields are not shown; a stock shortage blocks only
a document that moves stock (`update_stock = 0` is information); a chart is drawn only when it has at least two
non-zero points. Drill-down stays inside the view: trial balance row or finding -> general ledger of that account and
period -> document -> line details, with a back button at every step.
