# Server scripts (customize.request_server_script) — Frappe v16 safe_exec cheat sheet

Verified against `frappe/utils/safe_exec.py`, `frappe/core/doctype/server_script/server_script.py` and
`server_script_utils.py` (Frappe 16.28), and by listing `get_safe_globals()` on this bench.

Arguments: `operation` (`create` | `update` | `enable` | `disable`), `name`, `script_type` (`DocType Event` | `API`),
`reference_doctype` + `doctype_event` (DocType Event), `api_method` (API), `script`, `enable_rate_limit`,
`rate_limit_count`, `rate_limit_seconds`, `purpose`. `allow_guest` is always 0.

## Types offered here, and why not the others

| Type | Offered | Why |
|---|---|---|
| DocType Event | yes | validate / compute / block on save, submit, cancel, delete — runs everywhere (form, import, REST) |
| API | yes, logged-in users only | an endpoint at `/api/method/<api_method>` for a client script or an integration |
| Scheduler Event | no | runs as Administrator with no user: every read in it ignores permissions |
| Permission Query | no | its `conditions` string is glued into every list query; a mistake widens access |
| Workflow Task, payment events, rename, discard | no | outside v1 |

Preconditions: server scripts are enabled for the bench (`bench set-config -g server_script_enabled 1`, read from
`common_site_config.json` only), and the approver holds **Script Manager** (`ServerScript.validate` calls
`frappe.only_for("Script Manager")`).

## DocType events (doctype_event → document method)

| doctype_event | runs at | can change `doc` | can stop with `frappe.throw` |
|---|---|---|---|
| Before Insert | `before_insert` (new documents only) | yes | yes |
| Before Validate | `before_validate` | yes | yes |
| Before Save | `validate` (every save, before writing) | yes | yes |
| After Insert | `after_insert` | no (already written) | rolls back the save |
| After Save | `on_update` | no | rolls back the save |
| Before Submit / After Submit | `before_submit` / `on_submit` | before: yes | yes |
| Before Cancel / After Cancel | `before_cancel` / `on_cancel` | before: yes | yes |
| Before Delete / After Delete | `on_trash` / `after_delete` | — | yes |
| Before Save (Submitted Document) / After Save (Submitted Document) | `before_update_after_submit` / `on_update_after_submit` | only `allow_on_submit` fields | yes |
| Before Print | `before_print` | for the print only | — |

- Most rules belong in **Before Save** ("validate"). Use Before Submit for rules that only matter when posting.
- In "After" events, setting `doc.x = ...` does not save it; avoid writing there.
- Several enabled scripts on the same DocType + event all run, in no guaranteed order (the card notes them).
- Not run during `bench migrate` or install.

## What a DocType Event script has

- `doc`: the document being saved (a full Document: `doc.items`, `doc.get("field")`, `doc.customer`).
- `frappe.db.commit`, `frappe.db.rollback`, `frappe.db.add_index` are **removed** in document events.
- No return value; to stop, `frappe.throw(_("message"))`. To inform, `frappe.msgprint(_("message"))`.

## What an API script has

- Arguments in `frappe.form_dict` (`frappe.form_dict.customer`, `frappe.form_dict.get("from_date")`).
- The answer: `frappe.response["message"] = {...}` (or set keys on `frappe.flags`, which are returned instead).
- URL: `/api/method/<api_method>`; from a client script: `frappe.xcall("<api_method>", {customer: frm.doc.customer})`.
- Runs as the calling user. `enable_rate_limit: 1` (default suggestion) with `rate_limit_count` per
  `rate_limit_seconds` (Frappe default 5 per day when unset — set both).
- No `doc`.

## The language: RestrictedPython

Forbidden, the checker refuses and Frappe fails at run time:
- `import` / `from … import` → `ImportError: __import__ not found`. Everything below is already available.
- Top-level `return`; decorators (`@frappe.whitelist()`); wrapping the logic in a `def` that nothing calls.
- `.format()` and `.format_map()` (blocked attributes) → use f-strings: `f"{doc.name} is over {limit}"`.
- Any name or attribute starting with `_` (`doc._meta`, `__class__`); `getattr` games; `exec`/`eval`/`open`.
- `print` writes to the log only.

Builtins: `abs all any bool bytes callable chr complex dict divmod enumerate float hash hex id int isinstance
issubclass len list max min oct ord pow range repr round set slice sorted str sum tuple zip`, the standard exceptions,
`True False None`, plus `_` (translate), `json.loads/dumps`, `as_json`, `scrub`, `html2text`.

## frappe.* available, and whether it checks permissions

| Call | Permissions | Use |
|---|---|---|
| `frappe.get_list(doctype, filters=…, fields=[…], limit_page_length=…)`, `frappe.db.get_list` | **yes**, as the session user | the default for reading other records |
| `frappe.get_all`, `frappe.db.get_all` | **no** | avoid in API scripts |
| `frappe.db.get_value(doctype, name_or_filters, field)` | **no** | fine for the same document's links in an event; in API scripts check first |
| `frappe.get_doc(doctype, name)`, `frappe.get_cached_doc`, `frappe.get_last_doc` | **no** | then call `d.check_permission("read")` (raises if not allowed) |
| `frappe.db.exists`, `frappe.db.count`, `frappe.db.get_single_value`, `frappe.db.get_default` | **no** | |
| `frappe.qb` | **no** | query builder; reads only here |
| `frappe.db.sql(query, values)` | **no**, select/explain only | refused by the checker: use a Query Report |
| `frappe.new_doc`, `frappe.copy_doc`, `frappe.get_mapped_doc`, `.insert()` / `.save()` / `.submit()` on other docs, `frappe.db.set_value` | runs with full rights | side effects outside the document: warned, keep minimal |
| `frappe.get_meta(doctype)` | — | field definitions |
| `frappe.session.user`, `frappe.user`, `frappe.get_fullname(user)` | — | who is running it |
| `frappe.msgprint`, `frappe.throw`, `frappe.bold`, `frappe.log_error(title, message)` | — | messages and logging |
| `frappe.utils.*` (below), `frappe.format_value`, `frappe.render_template` | — | formatting |

There is **no** `frappe.has_permission` and no `frappe.get_roles` inside a server script. To read only what the user
may read, use `frappe.get_list` with a `name` filter, or `frappe.get_doc(...)` followed by `.check_permission("read")`.

Refused by the checker (and why): `frappe.make_get_request/post/put/patch/delete_request`, `FrappeClient` (data leaves
the system); `frappe.sendmail`, `frappe.enqueue`, `frappe.call`, `run_script`, `frappe.get_print`,
`frappe.attach_print` (indirect execution, mail); `frappe.delete_doc`, `frappe.rename_doc`, `frappe.db.add_index`;
`frappe.db.commit/rollback`; `ignore_permissions` in any form (`ignore_permissions=True`, `doc.flags.ignore_permissions`);
`frappe.set_user`, `frappe.local`, writing `frappe.session`; `frappe.db.sql`; any literal protected DocType (User, Role,
Has Role, User Permission, DocPerm, Custom DocPerm, Server Script, Client Script, Webhook, Email Account, OAuth…,
AI Settings, any `… Settings`, Company, Fiscal Year, Accounting Period); `<script` or `on…=` in messages.

`frappe.utils` (the safe set): `nowdate`, `today`, `now`, `now_datetime`, `nowtime`, `getdate`, `get_datetime`,
`add_days`, `add_months`, `add_years`, `add_to_date`, `date_diff`, `month_diff`, `time_diff_in_hours`,
`get_first_day`, `get_last_day`, `get_year_start`, `get_year_ending`, `formatdate`, `format_date`, `flt`, `cint`,
`cstr`, `rounded`, `floor`, `ceil`, `fmt_money`, `money_in_words`, `strip_html`, `escape_html`, `comma_and`,
`get_url_to_form`, `get_link_to_form`, `parse_json`, `sha256_hash`, `unique`, `evaluate_filters`.

## Errors seen on real sites and what they mean

| Message | Cause | Fix |
|---|---|---|
| `ImportError: __import__ not found` | an `import` line | delete it |
| `'return' outside function` / invalid python at line N | top-level `return` | use `frappe.throw` (events) or `frappe.response["message"]` (API) |
| `format is an unsafe attribute` | `"...".format()` | f-string |
| `Please set the document name` | created without `name` (autoname Prompt) | the tool names it `<DocType> - <purpose>` |
| `[Server Script, X]: script` (mandatory) | invented fields (`code`, `api_method_name`) left `script` empty | pass `script` |
| `Duplicate entry '<api_method>'` | another API script has that method | choose another `api_method` |
| `Server Scripts are disabled` | `server_script_enabled` not set for the bench | administrator: `bench set-config -g server_script_enabled 1` |

## Example 1 — block invoices with a discount above 20% (DocType Event, Sales Invoice, Before Save)

```python
limit = 20
discount = frappe.utils.flt(doc.additional_discount_percentage)
if discount > limit:
    frappe.throw(f"Discount {discount}% is above the allowed {limit}%")

for row in doc.items:
    if frappe.utils.flt(row.discount_percentage) > limit:
        frappe.throw(f"Row {row.idx}: discount {row.discount_percentage}% is above {limit}%")
```

(`"...".format()` is not allowed; build messages with f-strings.)

## Example 2 — a customer's outstanding balance, only for users who may read the customer (API)

`api_method: "customer_outstanding"`, `enable_rate_limit: 1`, `rate_limit_count: 60`, `rate_limit_seconds: 60`.

```python
customer = frappe.form_dict.get("customer")
if not customer:
    frappe.throw(_("customer is required"))

# get_list applies the caller's permissions: nothing comes back if they may not read this customer
allowed = frappe.get_list("Customer", filters={"name": customer}, pluck="name", limit_page_length=1)
if not allowed:
    frappe.throw(_("Not permitted"), frappe.PermissionError)

rows = frappe.get_list(
    "Sales Invoice",
    filters={"customer": customer, "docstatus": 1, "outstanding_amount": [">", 0]},
    fields=["name", "posting_date", "outstanding_amount"],
    order_by="posting_date asc",
    limit_page_length=100,
)
frappe.response["message"] = {
    "customer": customer,
    "invoices": rows,
    "total": sum(frappe.utils.flt(r.outstanding_amount) for r in rows),
}
```

Called from a client script: `frappe.xcall("customer_outstanding", {customer: frm.doc.customer}).then((r) => …)`.
