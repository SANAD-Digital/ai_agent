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
action requests are enabled, they create an `AI Approval Request`; a permitted
human must approve it in Frappe before execution.

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
