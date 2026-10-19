---
name: customization
title: Customization
description: Load when the user wants to change how the system itself works or looks — not to enter data: add or change a field, make a field mandatory/hidden/read-only/bold, rename a label, show a column in a list or a standard filter, change a form's title/search/sort/quick entry, add behaviour to a form or list (client script), validate or compute on save or expose an API (server script), or build a custom report. Not for creating or editing documents (documents skill).
tools: customize.inspect, customize.get, customize.check, customize.request_field, customize.request_properties, customize.request_client_script, customize.request_server_script, customize.request_report, customize.request_undo, schema.search_doctypes, schema.describe_doctype, data.query, data.count, skills.read_reference
---

# Customization (Frappe / ERPNext v16)

Every change here alters the system for everyone. Each `customize.request_*` creates one approval card that shows the
diff, the full code, the checks and the undo plan; a person with the right Frappe roles approves it in ERPNext. Nothing
is ever applied automatically, and there is no "always approve" for these requests.

## 1. Pick the least powerful tool that does the job

| The user wants | Use |
|---|---|
| Show, hide, require, make read-only, bold or relabel a field; change its default, description or `depends_on` | `customize.request_properties` (field_properties) |
| A column in the list view / a filter in the list sidebar | `request_properties` with `in_list_view` / `in_standard_filter` (+ `columns` for width) |
| Form title, link search, default sort, quick entry, track changes | `request_properties` (doctype_properties) |
| A new field | `customize.request_field` (operation add) |
| A report | `customize.request_report` — **Report Builder first**; Query Report only for joins between unrelated DocTypes or aggregates the builder cannot do |
| Behaviour on the screen (set a value, filter a link, a button, show/hide live) | `customize.request_client_script` |
| A rule enforced everywhere (screen, import, REST): validate, compute, block | `customize.request_server_script` with `script_type: "DocType Event"` |
| An endpoint for a client script or an integration | `customize.request_server_script` with `script_type: "API"` |

Order of power: property < field < Builder report < client script < server script. Go down only when the step above
cannot do it, and say why in `purpose`.

## 2. Workflow

1. `customize.inspect {doctype}` first: what is already customized, whether the request already exists, whether the
   DocType can be customized at all (core, Single and custom DocTypes cannot go through Customize Form).
2. `schema.describe_doctype {doctype, fields_filter:[...]}` for exact fieldnames. Never invent one.
3. Design the change, then `customize.check {kind, arguments}` until `valid` is true. On any `critical` check: fix it
   and check again. Never send a request you know fails.
4. Send the matching `customize.request_*` **once**, with `purpose` (one line, in the user's language: why).
5. Tell the user what the card will show and that they approve it in ERPNext. Stop and wait.
6. After it ran: say how to see it working (open form X, save, look at list Y) and that "undo" makes a new request
   (`customize.request_undo {approval}`).

Editing an existing script or report: read it with `customize.get {kind, name}` first, change only what was asked,
send the whole new source with `operation: "update"`.

## 3. Fixed rules

- Never use `actions.request_*` for Custom Field, Property Setter, Client/Server Script, Report, Print Format,
  Workflow, DocType or anything in their family: those requests are refused. Use these tools.
- Fieldnames are English `snake_case` ASCII (`custom_` is added if missing); labels are in the user's language.
- One script per purpose. Name it `<DocType> - <purpose>`; the tool adds the provenance header.
- Do not offer what these tools cannot do: Scheduler Event, Permission Query, Web Page/Web Form, Print Format,
  Notification, Workflow, new DocTypes, roles and permissions. Explain the alternative, or that an administrator adds it
  by hand in ERPNext.
- Server scripts never send data out: no HTTP requests, no email, no enqueue, no `ignore_permissions`. The checker
  refuses them; do not try to work around it.
- A client script is convenience, not protection: data rules that must hold on import or REST go in a server script.
- When the user only asks "can it be done?", answer and offer; do not create a request unasked.

## 4. Mistakes seen on real sites (do not repeat)

- `import frappe` / `from x import y` in a server script → `ImportError: __import__ not found`. `frappe`, `doc` and
  the safe helpers are already there.
- `return` at the top level of a server script, `@frappe.whitelist()`, a `def` wrapping the main logic. An API script
  answers with `frappe.response["message"] = ...`.
- `"...".format(...)` → fails at run time (`format` is blocked). Use f-strings.
- Invented Server Script fields (`code`, `api_method_name`, `created_by`): the script ended up empty.
- `%(from_date)s` in a query with no `from_date` filter → `KeyError: b'from_date'`. Every `%(x)s` needs a filter named
  `x`, and every filter should be used.
- Arabic fieldnames (`custom_اسم_العميل`), spaces in fieldnames, fields named `name`, `owner`, `docstatus`, `idx`.
- Making an existing field mandatory without telling the user that every old document will ask for it on next save.

## 5. References

`skills.read_reference {skill_name:"customization", reference:"<name>"}`:

| Reference | When |
|---|---|
| `fields` | field types and their options, fetch_from, depends_on syntax, permlevel, reserved names, removing fields |
| `properties` | every property Customize Form accepts, what Frappe refuses, recipes (list columns, filters, title, search, quick entry) |
| `server-scripts` | the v16 safe_exec cheat sheet: events, what is available, permissions of each read, API scripts, full examples |
| `client-scripts` | form and list events, frm API, child tables, list settings, what the checker refuses |
| `reports` | Builder vs Query, the Builder JSON, query parameters, filters and columns, roles and the permission warning |
