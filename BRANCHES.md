# Branches and Frappe versions

| Branch | Frappe / ERPNext | Python | What it is |
|---|---|---|---|
| `main` | 16 | 3.14 | Where all work happens. |
| `version-16` | 16 | 3.14 | What Frappe 16 sites install. Follows `main`. |
| `version-15` | 15 | 3.10–3.14 (CI on 3.11, test bench on 3.12) | What Frappe 15 sites install. Takes `main` by merge. |

Install on a Frappe 15 bench:

```
bench get-app --branch version-15 https://github.com/SANAD-Digital/ai_agent
bench --site <site> install-app ai_agent
```

## How a change travels

1. Commit to `main`, whatever the change is, unless it exists only because of Frappe 15.
2. Move `version-16` to `main`: `git push origin main:version-16`.
3. Merge into `version-15`: `git checkout version-15 && git merge main`.
4. CI runs the tests on Frappe 15 for every push to `version-15` (`.github/workflows/ci_v15.yml`). Open the
   screens the change touched on a Frappe 15 site too: tests alone do not catch layout differences.
5. Never merge `version-15` back into `main`.

A fix that works on both versions goes on `main`. What only Frappe 15 needs goes in `ai_agent/compat_v15.py` or
`ai_agent/compat_v15_oauth.py`, on `version-15`, so the rest of the code stays identical and merges stay clean.

## What exists only on `version-15`

- `ai_agent/compat_v15.py`, `ai_agent/compat_v15_oauth.py`, and the line in `ai_agent/__init__.py` that loads them.
- Two lines at the end of `hooks.py` (`page_renderer`, `after_request`) for the OAuth documents.
- `pyproject.toml`: `frappe = ">=15.0.0,<16.0.0"`, `requires-python = ">=3.10"`.
- `.github/workflows/ci_v15.yml`.

`compat_v15.py` makes Frappe 15 behave like 16 where this app's code relies on it. Each change applies to the whole
process, so other apps on the site see it too; each is a fix or an addition Frappe 16 already has.

| Frappe 15 behaviour | Effect without the fix |
|---|---|
| `frappe.qb.get_query` takes no `user` / `ignore_permissions` and applies no permissions | `data.query` fails; a raw query would ignore the user's access |
| A child-table `get_list` with `parent_doctype` joins the parent as another child and checks only the role | Metrics by item fail; a user limited to one company could read another's lines |
| A filter through a Link (`account.root_type`) is read as a column name | Default metrics fail |
| `get_list` has no `{"COUNT": "name", "as": "n"}` | Counts and sums fail |
| `cache.get_value` has no `use_local_cache`; `set_value` with an expiry skips the local copy | The chat's Stop button never stops a run |
| `frappe.only_for` lets every user through while tests run | Tests cannot show an endpoint refuses a user (tests only) |
| `CustomizeForm.allow_property_change` takes three arguments | Every property change is refused |
| `frappe.client_cache`, `std_rights`, `RESERVED_KEYWORDS`, `TimespanOptions`, `frappe.tests.IntegrationTestCase` do not exist | Imports fail |

`compat_v15_oauth.py` adds what AI tools need to sign in to the MCP server (Claude, ChatGPT, Claude Code, Cursor,
VS Code): the OAuth discovery documents (`/.well-known/oauth-authorization-server`,
`/.well-known/oauth-protected-resource`), dynamic client registration, and CORS answers for ChatGPT. Frappe 15 has
the OAuth server itself (authorize, token with PKCE); it has no OAuth Settings, so these are on whenever
"Allow External AI Tools" is on in AI Settings.

## What to watch when writing code for `main`

- **Queries:** `frappe.qb.get_query(..., ignore_permissions=False, user=...)` works through the shim; a new Frappe 16
  query argument does not. A `{"FUNC": ...}` field works for simple aggregates only.
- **Cache:** `frappe.client_cache` is plain `frappe.cache` on 15.
- **OAuth Settings:** read it only through `ai_agent.mcp.setup` (`oauth_ready`, `chatgpt_allowed`, `enable_oauth`).
- **Workspaces:** read who owns one through `workspace_spec.workspace_row` / `app_workspaces`; Workspace has no
  `standard` on 15.
- **Comments:** Frappe 15 stores a comment's quotes as `&quot;`; unescape what you parse back.
- **New fields of Frappe's own DocTypes** (List View Settings, OAuth Client, Workspace …): check 15 has them.

## Known gaps on Frappe 15

- `customize.request_workspace` is not offered: it is built on Frappe 16's module sidebars and desktop icons.
  Number cards and charts still go on workspaces through `customize.request_dashboard`.
- The app's module sidebar (`ai_agent/ai_agent/sidebar/`) and `desktop_icon/` mean nothing on 15; the AI workspace
  links every screen.
- List settings cannot set `show_tags` or `disable_scrolling` (15's List View Settings has neither).
- 15's OAuth server does not check a client secret at the token endpoint; PKCE (S256) protects the code exchange.
