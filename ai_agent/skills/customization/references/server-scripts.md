# Server scripts (customize.request_server_script) — Frappe v16

Verified against the installed Frappe 16.28 (`frappe/utils/safe_exec.py`, `core/doctype/server_script/`,
`model/document.py`, `handler.py`) and by running every example below through `safe_exec` and the checker. Where
the official docs disagree with the source, the source wins and the line is marked **(docs differ)**.

Arguments: `operation` (`create` | `update` | `enable` | `disable`), `name`, `script_type` (`DocType Event` | `API`),
`reference_doctype` + `doctype_event` (DocType Event), `api_method` (API, `^[a-z][a-z0-9_.]{2,80}$`), `script`,
`enable_rate_limit`, `rate_limit_count`, `rate_limit_seconds`, `purpose`. `allow_guest` is forced to 0. There is
no `code`, `api_method_name`, `module` or `event_frequency` argument: the source goes in `script`.

Preconditions: server scripts are enabled for the bench (`server_script_enabled` in `common_site_config.json`, not
in site_config), and the approver holds **Script Manager** (`ServerScript.validate` → `frappe.only_for("Script Manager")`).

## 1. Script types

| Type | Through this tool | What it is |
|---|---|---|
| DocType Event | yes | runs inside `doc.insert/save/submit/cancel/delete` everywhere: form, import, REST, other code |
| API | yes, signed-in users only | the script is the endpoint `/api/method/<api_method>` |
| Scheduler Event | no (an administrator adds it by hand) | runs as Administrator on a frequency or cron; every read ignores permissions |
| Permission Query | no | sets `conditions` (SQL WHERE text) that Frappe adds to every list query of `reference_doctype` |
| Workflow Task | no | runs from a Workflow Action Master with `doc` |

## 2. DocType events (v16 `EVENT_MAP`)

The server script runs **after** the DocType's own controller method and the app hooks of the same event
(`Document.run_method`).

| doctype_event | document method | when | change `doc`? |
|---|---|---|---|
| Before Insert | `before_insert` | new docs only, before the name is set (`doc.name` empty) | yes |
| Before Validate | `before_validate` | save and submit, before the controller's `validate` | yes, ERPNext recalculates after it |
| Before Save | `validate` | every save **and every submit**, after the controller computed totals | yes, totals are not recomputed |
| After Insert | `after_insert` | new docs, after the row is written, before After Save | no (already written) |
| After Save | `on_update` | every save and submit, after writing | no |
| Before Submit / After Submit | `before_submit` / `on_submit` | submit | before: yes |
| Before Cancel / After Cancel | `before_cancel` / `on_cancel` | cancel | before: yes |
| Before Delete / After Delete | `on_trash` / `after_delete` | delete | — |
| Before Save (Submitted Document) / After Save (Submitted Document) | `before_update_after_submit` / `on_update_after_submit` | "Update" of a submitted doc | only `allow_on_submit` fields |
| Before Print | `before_print` | rendering a print | for the print only |

Not offered here although v16 has them: Before/After Rename, Before/After Discard, the six On Payment events.
**(docs differ: they list only the 14 offered events)**

Order of a new save: Before Insert → Before Validate → Before Save → (mandatory and link checks, write) → After
Insert → After Save. Submit: Before Validate → Before Save → Before Submit → write → After Save → After Submit.

- Most rules belong in **Before Save**. To change an input ERPNext must recalculate from (qty, rate, discount),
  use **Before Validate**. Before Save can still fill a mandatory field (mandatory checks run after it).
- "Before Save" is the controller's `validate`, not its `before_save` method. **(docs differ: not stated)**
- In After events `doc.x = ...` is never stored; never call `doc.save()` on `doc` (recursion).
- Several enabled scripts on the same DocType + event all run, in creation order. Not run during migrate/install.
- `frappe.throw(msg)` stops the action and rolls back the whole transaction (also from After events).

### What `doc` is

The real `Document` (not a dict): `doc.fieldname`, `doc.get("f")`, `doc.items` (rows are Documents with `row.idx`,
`row.doctype`, `row.name`), `doc.append("items", {...})`, `doc.is_new()`, `doc.has_value_changed("status")`,
`doc.get_doc_before_save()` (None for a new doc), `doc.get_value_before_save("status")`,
`doc.check_permission("read")`, `doc.meta.get_field("x")`, `doc.docstatus` (0 draft, 1 submitted, 2 cancelled).
In DocType Events `frappe.db.commit`, `frappe.db.rollback` and `frappe.db.add_index` are removed.

## 3. API scripts

- Input: `frappe.form_dict` (query string or JSON body): `frappe.form_dict.get("customer")`. Values arrive as
  strings: convert with `frappe.utils.cint / flt / getdate`.
- Output: `frappe.response["message"] = value`. If the script also sets keys on `frappe.flags`, Frappe returns the
  flags **instead** (`handler.run_server_script` returns them when not empty): never set `frappe.flags` in an API
  script. **(docs differ: they say flags are not used for API responses)**
- URL `/api/method/<api_method>`; from a client script `frappe.xcall("<api_method>", {...})`.
- Runs as the calling user. With `enable_rate_limit: 1` the limit is `rate_limit_count` calls per
  `rate_limit_seconds` (Frappe default when unset: 5 per day): always send both.
- No `doc`. One script per `api_method` (a duplicate is critical).

## 4. The language (RestrictedPython with Frappe's `FrappeTransformer`)

| Not allowed | Error you get | Write instead |
|---|---|---|
| `import x`, `from x import y` | `ImportError: __import__ not found` | nothing: `frappe`, `json`, `_` are there |
| top-level `return` | `'return' outside function` | `frappe.throw(...)` (events) / `frappe.response["message"] = ...` (API) |
| `@frappe.whitelist()`, a `def` holding all the logic | the endpoint runs nothing | top-level code |
| `"...".format(...)`, `.format_map` | `format is an unsafe attribute` | f-string, or `"%s" % x` |
| `d["k"] += 1`, `doc.total += x` | `Augmented assignment of object items and slices / of attributes is not allowed` | `d["k"] = d.get("k", 0) + 1`, `doc.total = doc.total + x` (`n += 1` on a plain name works) |
| `a, b = 1, 2`, `x, y = f()` (unpacking in an assignment) | `NameError: name '_unpack_sequence_' is not defined` | `pair = f()` then `pair[0]`; unpacking **in a for loop** works: `for k, v in d.items():` |
| names or attributes starting with `_` (`doc._meta`, `frappe._dict`) | `"_dict" is an invalid attribute name` | `{}` or the top-level `_dict(...)` |
| `getattr`, `hasattr`, `type`, `map`, `filter`, `reversed`, `next`, `iter`, `open`, `eval` | `NameError` | comprehensions, `sorted(..., reverse=True)`, `isinstance` |
| `some_date.strftime(...)` | `KeyError: '__import__'` | `frappe.utils.formatdate(d, "dd-MM-yyyy")`, `frappe.utils.getdate(d).year` |
| arithmetic on an empty field | `TypeError: unsupported operand type(s) for *: 'NoneType' and 'float'` | `frappe.utils.flt(doc.x)`, `cint(...)` |

Builtins: `abs all any bool bytes callable chr complex dict divmod enumerate float hash hex id int isinstance
issubclass len list max min oct ord pow range repr round set setattr slice sorted str sum tuple zip`, the standard
exceptions, `True False None`. Top-level names: `_` (translate), `_dict`, `json` (`loads`/`dumps`), `as_json`,
`scrub`, `html2text`. `def`, `lambda`, comprehensions, generators, `try/except` and f-strings work. `print()` writes
to the log only.

## 5. The `frappe` namespace inside a server script (v16 exec mode)

A name missing from this table does not exist: calling it fails at run time with `AttributeError: module has no
attribute '<name>'`. **There is no `frappe.has_permission`, `frappe.get_roles`, `frappe.get_cached_value`,
`frappe.get_single`, `frappe.local`, `frappe.conf`.** **(docs differ: the Script API page does not say so)**

| Call (signature) | Checks permissions | Notes |
|---|---|---|
| `frappe.get_list(doctype, filters=None, fields=None, order_by=None, group_by=None, limit=None, pluck=None)` (= `frappe.db.get_list`) | **yes**, session user | the default for reading other records |
| `frappe.get_all(...)` (= `frappe.db.get_all`) | **no** | same arguments; info in events, warning in APIs |
| `frappe.db.get_value(doctype, name_or_filters, fieldname_or_list, as_dict=False)` | no | one value or one row |
| `frappe.db.get_single_value(doctype, field)`, `frappe.db.get_default(key)` | no | `… Settings` DocTypes are refused by the checker |
| `frappe.db.exists(doctype, name_or_filters)` → name or None, `frappe.db.count(doctype, filters)` | no | |
| `frappe.get_doc(doctype, name)`, `frappe.get_doc({...})`, `frappe.get_cached_doc`, `frappe.get_last_doc(doctype, filters=None)` | no | follow with `d.check_permission("read")` (raises PermissionError) |
| `frappe.new_doc(doctype)`, `frappe.copy_doc(doc)`, `frappe.get_mapped_doc(...)`, `.insert()` `.save()` `.submit()` `.cancel()` on other docs, `frappe.db.set_value(dt, name, field, value)` | as the user | side effects: warned on the card, keep minimal |
| `frappe.get_meta(doctype)` | — | `frappe.get_meta("Item").get_field("uom").options` |
| `frappe.session.user`, `frappe.user` (a string), `frappe.get_fullname(user)`, `frappe.full_name`, `frappe.lang` | — | |
| `frappe.form_dict`, `frappe.request`, `frappe.response` | — | API scripts |
| `frappe.throw(msg, exc=frappe.ValidationError, title=None)`, `frappe.msgprint(msg, title=None, indicator=None, alert=False)`, `frappe.bold(text)` | — | messages; `_("text")` to translate |
| `frappe.format_value(value, {"fieldtype": "Currency"})` (= `frappe.format`), `frappe.format_date(d)`, `frappe.get_url()`, `frappe.get_system_settings(field)` | — | |
| `frappe.ValidationError`, `frappe.PermissionError`, `frappe.DoesNotExistError`, `frappe.DuplicateEntryError`, `frappe.MandatoryError`, … | — | every `frappe.exceptions` class |
| `frappe.log_error(title, message)` | — | writes an Error Log |
| `frappe.qb`, `frappe.db.sql(select…)`, `frappe.db.escape` | — | exist, **refused by the checker** (no row permissions): use a Query Report |
| `frappe.sendmail`, `frappe.enqueue`, `frappe.call`, `run_script`, `frappe.make_get/post/put/patch/delete_request`, `FrappeClient`, `frappe.get_print`, `frappe.attach_print`, `frappe.render_template`, `frappe.publish_realtime` | — | exist, **refused**: data leaves the system or other code runs unreviewed |
| `frappe.delete_doc`, `frappe.rename_doc`, `frappe.db.add_index`, `frappe.db.commit/rollback`, `frappe.set_user`, writing `frappe.session` | — | **refused** |

Also refused: `ignore_permissions` / `ignore_user_permissions` in any form (keyword, `doc.flags.…`, inside a dict);
any literal protected DocType (User, Role, Has Role, Role Profile, User Permission, DocPerm, Custom DocPerm, Server
Script, Client Script, Webhook, Email Account, OAuth…, any `… Settings`, Company, Fiscal Year, Accounting Period,
AI …); `<script` or `on…=` inside messages.

**v16 aggregates in `get_list`/`get_all`**: `fields=["sum(grand_total) as total"]` fails with *SQL functions are not
allowed as strings in SELECT*. Use the dict form `{"SUM": "grand_total", "as": "total"}`, `{"COUNT": "*", "as": "n"}`
together with `group_by="customer"`. **(docs differ: older docs show string aggregates)**

`frappe.utils` (the safe subset; anything else is missing): dates `today` `nowdate` `now` `nowtime` `now_datetime`
`getdate` `get_datetime` `get_time` `add_days` `add_months` `add_years` `add_to_date` `date_diff` `month_diff`
`time_diff` `time_diff_in_hours` `time_diff_in_seconds` `get_first_day` `get_last_day` `get_first_day_of_week`
`get_last_day_of_week` `get_quarter_start` `get_quarter_ending` `get_year_start` `get_year_ending` `get_weekday`
`get_timespan_date_range` `formatdate` `format_date` `format_datetime` `format_time` `format_duration`
`pretty_date` `global_date_format`; numbers `flt` `cint` `cstr` `rounded` `floor` `ceil` `safe_div` `remainder`
`fmt_money` `money_in_words` `in_words`; text `strip_html` `escape_html` `strip` `comma_and` `comma_or` `unique`
`has_common` `sha256_hash` `generate_hash` `parse_json` `markdown`; links `get_url` `get_url_to_form`
`get_url_to_list` `get_link_to_form` `get_url_to_report`; filters `evaluate_filters` `compare`.

## 6. Examples (each passes the checker and runs in `safe_exec`)

### Block an over-limit discount — Sales Invoice, Before Save

```python server-script type=event doctype="Sales Invoice" event="Before Save"
limit = 20
discount = frappe.utils.flt(doc.additional_discount_percentage)
if discount > limit:
    frappe.throw(f"Discount {discount}% is above the allowed {limit}%")

for row in doc.items:
    if frappe.utils.flt(row.discount_percentage) > limit:
        frappe.throw(f"Row {row.idx}: discount {row.discount_percentage}% is above {limit}%")
```

### Fill an input before ERPNext recalculates — Sales Order, Before Validate

```python server-script type=event doctype="Sales Order" event="Before Validate"
for row in doc.items:
    if not row.delivery_date and doc.delivery_date:
        row.delivery_date = doc.delivery_date
```

### Unique tax ID and a change notice — Customer, Before Save

```python server-script type=event doctype="Customer" event="Before Save"
tax_id = (doc.tax_id or "").strip().upper()
doc.tax_id = tax_id or None

if tax_id and frappe.db.exists("Customer", {"tax_id": tax_id, "name": ["!=", doc.name]}):
    frappe.throw(f"Tax ID {tax_id} is already used by another customer")

if not doc.is_new() and doc.has_value_changed("customer_group"):
    before = doc.get_value_before_save("customer_group")
    frappe.msgprint(f"Customer group changed from {before} to {doc.customer_group}", alert=True)
```

### Read a linked record — Sales Invoice, Before Submit

```python server-script type=event doctype="Sales Invoice" event="Before Submit"
group = frappe.db.get_value("Customer", doc.customer, "customer_group")
if group == "Wholesale" and not doc.po_no:
    frappe.throw(_("Wholesale invoices need the customer's PO number"))
```

### Totals per key without `+=` on items — Sales Invoice, Before Save

```python server-script type=event doctype="Sales Invoice" event="Before Save"
qty_by_uom = {}
for row in doc.items:
    uom = row.uom or "-"
    qty_by_uom[uom] = qty_by_uom.get(uom, 0) + frappe.utils.flt(row.qty)

summary = ", ".join(f"{uom}: {qty}" for uom, qty in sorted(qty_by_uom.items()))
if summary and not doc.remarks:
    doc.remarks = f"Quantities: {summary}"
```

### A follow-up record — Customer, After Insert (a side effect: warned on the card)

```python server-script type=event doctype="Customer" event="After Insert"
todo = frappe.new_doc("ToDo")
todo.description = f"Welcome call for new customer {doc.customer_name}"
todo.reference_type = "Customer"
todo.reference_name = doc.name
todo.allocated_to = frappe.session.user
todo.insert()
```

### API: a customer's open invoices, only if the caller may read the customer

`api_method: "customer_outstanding"`, `enable_rate_limit: 1`, `rate_limit_count: 60`, `rate_limit_seconds: 60`.

```python server-script type=api method=customer_outstanding args="customer=@Customer"
customer = frappe.form_dict.get("customer")
if not customer:
    frappe.throw(_("customer is required"))

# get_list applies the caller's permissions: nothing comes back if they may not read this customer
allowed = frappe.get_list("Customer", filters={"name": customer}, pluck="name", limit=1)
if not allowed:
    frappe.throw(_("Not permitted"), frappe.PermissionError)

rows = frappe.get_list(
    "Sales Invoice",
    filters={"customer": customer, "docstatus": 1, "outstanding_amount": [">", 0]},
    fields=["name", "posting_date", "due_date", "outstanding_amount"],
    order_by="posting_date asc",
    limit=100,
)
frappe.response["message"] = {
    "customer": customer,
    "invoices": rows,
    "total": sum(frappe.utils.flt(r.outstanding_amount) for r in rows),
}
```

### API: net sales per customer in a period (v16 aggregate syntax)

`api_method: "sales_by_customer"`, rate limit as above.

```python server-script type=api method=sales_by_customer args="from_date=2026-01-01 to_date=2026-12-31"
today = frappe.utils.today()
from_date = frappe.utils.getdate(frappe.form_dict.get("from_date") or frappe.utils.get_first_day(today))
to_date = frappe.utils.getdate(frappe.form_dict.get("to_date") or today)
if from_date > to_date:
    frappe.throw(_("from_date is after to_date"))

rows = frappe.get_list(
    "Sales Invoice",
    filters={"docstatus": 1, "posting_date": ["between", [from_date, to_date]]},
    fields=["customer", {"SUM": "base_net_total", "as": "net_sales"}, {"COUNT": "*", "as": "invoices"}],
    group_by="customer",
    order_by="customer asc",
    limit=500,
)
frappe.response["message"] = rows
```

### Permission Query (reference only: the agent cannot request it)

Locals: `user` (the user being filtered) and `conditions` (set a SQL WHERE fragment; "" means no limit). The
checker refuses `frappe.db.escape` and `frappe.get_all("Has Role")`, so an administrator adds it by hand.

```python server-script type=permission_query doctype="ToDo"
roles = frappe.get_all("Has Role", filters={"parent": user, "parenttype": "User"}, pluck="role")
if "System Manager" not in roles:
    conditions = f"`tabToDo`.`allocated_to` = {frappe.db.escape(user)}"
```

## 7. Mistakes seen on real sites

| Seen | Cause | Fix |
|---|---|---|
| `ImportError: __import__ not found` (acc.dev `generate_prompt`, 3 tries) | `import frappe`, `from datetime import date` | delete imports; use `frappe.utils` |
| `invalid python at line 41: return {` (ai.dev) | top-level `return` in an API | `frappe.response["message"] = {...}` |
| `@frappe.whitelist() def generate_prompt(...)` as the whole API | the endpoint runs nothing | top-level code |
| `[Server Script, X]: script` mandatory (ai.dev, 5 tries) | sent `code` / `api_method_name` instead of `script` | the tool takes `script` |
| `Please set the document name` (acc.dev) | Server/Client Script inserted without a name | the tool names it `<DocType> - <purpose>` |
| `Duplicate entry 'generate_prompt'` | a second API script for the same method | `customize.inspect`, then `operation: update` |
| `module has no attribute 'has_permission'` (acc.dev Error Log) | `frappe.has_permission` | `frappe.get_list(..., filters={"name": x})` or `d.check_permission("read")` |
| `Augmented assignment of object items and slices is not allowed` (acc.dev, lines 121–123) | `totals[k] += v` | `totals[k] = totals.get(k, 0) + v` |
| `unsupported operand type(s) for *: 'NoneType' and 'float'` (sd.dev) | arithmetic on an empty field | wrap numbers in `flt()` |
| `frappe.get_single('AI Usage Dashboard')` then writing it (ai.dev) | missing function; writes to settings | not available; settings DocTypes are protected |
| `frappe.db.sql(...)` behind a dashboard API (ai.dev) | SQL in a script | a Query Report |
| `"Before Save"` used to change `qty` and the totals stayed old | totals were computed before the script | Before Validate |

Sources: https://docs.frappe.io/framework/user/en/desk/scripting/server-script ,
https://docs.frappe.io/framework/user/en/desk/scripting/script-api ,
https://docs.frappe.io/framework/user/en/api/database — verified against the Frappe 16.28 source on this bench.
