# AI Agent Standard Configuration

This app ships its baseline agent configuration as app-managed data so every customer site can be initialized without manually copying prompts, skills, or grants from another site.

## Source of Truth

- Standard agents are defined in `ai_agent/install.py` in `_standard_agents()`.
- Standard skills are stored as app files in `ai_agent/skills/*/SKILL.md`.
- Standard skill grants are created by `ai_agent/install.py` in `create_default_skill_grants()`.
- Safe AI Settings defaults are created by `ai_agent/install.py` in `create_default_settings()`.

## What Syncs Automatically

The app runs `ai_agent.install.ensure_standard_configuration` after install and after migrate.

It will:

- Create or refresh the standard agents.
- Sync every app-file skill from `SKILL.md`.
- Re-enable trusted app-file skills when they are part of the shipped baseline.
- Grant app-file skills to the standard agents for all users, except role-granted skills (below).
- Fill missing safe settings defaults.

It will not:

- Copy API keys or provider secrets.
- Overwrite configured provider credentials.
- Export customer-specific companies, users, roles, or private business data.

## Customer Deployment

Run one of these after installing or updating the app:

```bash
bench --site <site-name> migrate
```

Or run the sync directly:

```bash
bench --site <site-name> execute ai_agent.install.ensure_standard_configuration
```

## Adding a Standard Skill

1. Create a folder under `ai_agent/skills/<skill-name>/`.
2. Add `SKILL.md` with frontmatter:

```markdown
---
name: skill-name
description: Short description.
allow_implicit_invocation: 1
---
```

3. Put the agent instructions below the frontmatter.
4. Run migrate or execute `ai_agent.install.ensure_standard_configuration`.

The skill will then be available on every customer site after app update.

## Customization (customize.* tools)

The `customization` skill and the `customize.*` tools change the system itself: custom fields, field and form
properties (list columns, standard filters, title, search, quick entry), client scripts, server scripts (DocType
Event and API, never for guests) and custom reports (Report Builder and Query Report). Design:
`docs/customization/01-gap-audit-and-design.md`.

What the sync does:

- Syncs `ai_agent/skills/customization/SKILL.md` and its five references (`fields`, `properties`, `server-scripts`,
  `client-scripts`, `reports`) like every other app skill. The skill is loaded on demand, never always-on.
- Grants it **only by role**: one `AI Skill Grant` with `grant_type = Role`, `role = System Manager` on each standard
  agent that may act (ERP Operations Agent, ERP Power Operator). It is never granted to "All Users" and never to the
  read-only ERP Analyst (`install.ROLE_GRANTED_SKILLS`). An admin who disables that grant keeps it disabled.
- Seeds `customization_max_requests_per_hour = 20` once the field exists; every other customization setting stays at
  its default **off**.

AI Settings (section "Customization"), all off by default:

| Setting | Meaning |
|---|---|
| `allow_customization_actions` | master switch; nothing below works without it |
| `allow_customize_fields` | custom fields and field/form properties |
| `allow_customize_client_scripts` | client scripts (form and list) |
| `allow_customize_server_scripts` | server scripts; the approver also needs **Script Manager**, and `server_script_enabled` must be set for the bench (`bench set-config -g server_script_enabled 1`, read from common_site_config only) |
| `allow_customize_reports` | Report Builder and Query Report (Query Report needs Script Manager) |
| `customization_allow_mcp` | also offer the tools to external MCP clients (Claude, ChatGPT); requests only, approval stays in ERPNext |
| `customization_require_four_eyes` | the requester cannot approve their own customization |
| `customization_max_requests_per_hour` | per user (default 20) |

Who can use it: the user holds the Frappe roles the change needs (System Manager; plus Script Manager for server
scripts and Query Reports), the agent has `allow_actions = 1`, and the conversation is the Desk chat (MCP only with
`customization_allow_mcp`; the support bubble and WhatsApp never).

How approval works: every request is an `AI Approval Request` of type `Customize`. The card shows the before/after
diff, the **full code** with line numbers (and a diff on updates), every analyzer finding with its severity and line,
who and what it affects, and the undo plan. A critical finding hides the approve button. There is no "always approve"
and no automatic execution in any mode (auto mode, `mcp_require_approval = 0`). Over MCP the card has no approve
button: it links to the request in ERPNext. After execution the card shows the verification, a link to the created
record and an Undo action, which asks the assistant for a new (reverse) request.

Enable on a site, after the migration that adds the settings fields: turn on `allow_customization_actions` and the kinds you want in **AI Settings > Customization**, then try it with a
System Manager in the Desk chat ("add a Delivery Zone field to Sales Invoice").
