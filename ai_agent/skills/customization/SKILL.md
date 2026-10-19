---
name: customization
title: Customization
description: Load when the user wants to change how the system itself works or looks — not to enter data: add or change a field, make a field mandatory/hidden/read-only/bold, rename a label, show a column in a list or a standard filter, change a form's title/search/sort/quick entry, add behaviour to a form or list (client script), validate or compute on save or expose an API (server script), or build a custom report. Not for creating or editing documents (documents skill).
tools: customize.inspect, customize.get, customize.check, customize.request_field, customize.request_properties, customize.request_client_script, customize.request_server_script, customize.request_report, customize.request_undo, schema.search_doctypes, schema.describe_doctype, data.query, data.count, skills.read_reference
---

# Customization (Frappe / ERPNext v16)

Every change here alters the system for everyone. Each `customize.request_*` creates one approval card with the
diff, the full code, the checks and the undo plan; a person holding the needed Frappe roles approves it in ERPNext.
Nothing is applied automatically and there is no "always approve" for these requests.

**Read the matching reference before writing any code or field definition** — it has the exact v16 API, verified
examples and the errors seen on real sites:
`skills.read_reference {skill_name:"customization", reference:"<name>"}`.

| Reference | Read it before |
|---|---|
| `server-scripts` | any server script: events and what `doc` is, API scripts, the sandbox language rules, the full `frappe.*` list |
| `client-scripts` | any client script: form/child/list events and order, `frm` API, list settings, how scripts are loaded |
| `fields` | adding or changing a custom field: every fieldtype and its `options`, `depends_on`/`eval:`, `fetch_from`, naming |
| `properties` | Customize Form changes: allowed properties, what Frappe refuses on standard fields, recipes |
| `reports` | any report: Builder JSON, Query Report SQL and filters, roles, Script Report (later) |
| `print-formats-notifications` | questions about print formats, notifications, workflows (not offered yet) |

## 1. Pick the least powerful tool that does the job

| The user wants | Use |
|---|---|
| Show, hide, require, read-only, bold, relabel a field; its default, description or `depends_on` | `customize.request_properties` |
| A column in the list / a filter in the sidebar | `request_properties`: `in_list_view` (+ `columns`) / `in_standard_filter` |
| Form title, link search, default sort, quick entry, track changes | `request_properties` (doctype_properties) |
| A new field | `customize.request_field` (operation `add`) |
| A report | `customize.request_report`: **Report Builder first**; Query Report only for joins with other DocTypes or aggregates the Builder cannot do |
| Behaviour on the screen (set a value, filter a link, a button, show/hide live) | `customize.request_client_script` |
| A rule enforced everywhere (screen, import, REST): validate, compute, block | `customize.request_server_script`, `script_type: "DocType Event"` |
| An endpoint for a client script or an integration | `customize.request_server_script`, `script_type: "API"` |

Order of power: property < field < Builder report < client script < server script. Go down only when the step above
cannot do it, and say why in `purpose`.

## 2. Workflow

1. `customize.inspect {doctype}`: what is already customized (fields, property setters, scripts, reports), whether
   the request already exists, whether the DocType can be customized (core, Single, custom and protected DocTypes
   cannot).
2. `schema.describe_doctype {doctype, fields_filter:[...]}` for exact fieldnames and child table DocTypes. Never
   invent a fieldname, DocType or method.
3. Read the reference, design the change, then `customize.check {kind, arguments}` with the same arguments as the
   request. Repeat until `valid` is true; fix every `critical`, read every `warning`. Never send a request you know
   fails.
4. Send the matching `customize.request_*` **once**, with `purpose` (one line in the user's language: why).
5. Tell the user what the card shows and that they approve it in ERPNext. Stop and wait.
6. After it ran: say how to see it working (open form X, save, open list Y) and that undo is a new request
   (`customize.request_undo {approval}`).

Changing an existing script or report: `customize.get {kind, name}` first, change only what was asked, send the
whole new source with `operation: "update"`. A client script that calls an API server script needs that API to exist
first (otherwise the check is critical): request the server script, wait for approval, then the client script.

## 3. Fixed rules

- Never use `actions.request_*` for Custom Field, Property Setter, Client/Server Script, Report, Print Format,
  Workflow, DocType or their family: refused. Use these tools.
- Fieldnames are English ASCII `snake_case` (`custom_` is added); labels are in the user's language.
- One script per purpose, named `<DocType> - <purpose>`; the tool adds the provenance header.
- Not offered: Scheduler Event, Permission Query, Script Report, Print Format, Notification, Workflow, Web
  Page/Form, new DocTypes, roles and permissions. Explain the alternative or that an administrator adds it by hand.
- Server scripts never send data out or bypass permissions: no HTTP, mail, enqueue, `frappe.call`, SQL,
  `ignore_permissions`. The checker refuses them; do not work around it.
- A client script and `mandatory_depends_on` are convenience, not protection: rules that must hold on import or
  REST go in a Before Save server script.
- When the user only asks "can it be done?", answer and offer; do not create a request unasked.

## 4. The ten mistakes that broke real requests

1. `import …` in a server script → `ImportError: __import__ not found`. Everything is already there.
2. Top-level `return`, `@frappe.whitelist()`, or the logic inside an uncalled `def`. APIs set
   `frappe.response["message"]`.
3. `"…".format()`, `d[k] += 1`, `a, b = f()`, `frappe._dict`, `getattr`: all fail in the sandbox (see server-scripts §4).
4. `frappe.has_permission`, `frappe.get_roles`, `frappe.get_single`, `frappe.get_cached_value` do not exist in a
   server script.
5. `fields=["sum(x) as total"]` in `get_list` (v16 needs `{"SUM": "x", "as": "total"}`).
6. Invented argument names (`code`, `api_method_name`, `json_options`): the script/report ended up empty.
7. `%(x)s` in a Query Report without a **mandatory** filter `x` → `KeyError: b'x'`; `OR` without parentheses.
8. Arabic or label-derived fieldnames; Select options separated by commas; `depends_on` without `eval:`.
9. Changing the options of a standard Select, or un-requiring a standard mandatory field: Frappe refuses it.
10. Two client scripts on one DocType declaring the same top-level helper, or replacing ERPNext's
    `listview_settings` instead of merging.
