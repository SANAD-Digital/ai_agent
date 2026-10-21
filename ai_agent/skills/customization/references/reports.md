# Custom reports (customize.request_report) — Frappe v16

Verified against `frappe/core/doctype/report/report.py`, `frappe/desk/query_report.py`,
`public/js/frappe/views/reports/query_report.js` and `frappe/utils/safe_exec.py` (Frappe 16.28). Every example
passes the checker and runs read-only on a live site.

Arguments: `operation` (`create` | `update` | `disable` | `enable`), `report_name`, `ref_doctype`, `report_type`
(`Report Builder` | `Query Report` | `Script Report`), `builder` (Report Builder), `query` (Query Report),
`report_script` + `javascript` (Script Report), `filters`, `columns`, `roles`, `sample_filters` (values for the
preview run), `purpose`. `is_standard` is always "No"; `module` is the
module of `ref_doctype`. The report type cannot change on update: create a new report.

## 1. Builder, Query or Script?

| Need | Type | Why |
|---|---|---|
| rows of one DocType (+ its child tables), filters, sort, totals row | **Report Builder** (first choice) | runs through `frappe.get_list(..., user=viewer)`: every viewer sees only rows they may read; no Script Manager |
| a JOIN with another (non-child) DocType, grouped totals (count/sum per group), CTEs, UNION | **Query Report** | raw `frappe.db.sql(query, filters)`: **row permissions are not applied**; saving needs Script Manager |
| multi-select filters, a filter limited by another (`get_query`), permission-respecting reads across DocTypes, computed columns, optional filters | **Script Report** (§6) | Python run as the viewer (`frappe.get_list` applies their permissions) + JavaScript filters; saving needs Script Manager |

The checker says (info) when a query could have been a Builder report.

## 2. Report Builder: the `builder` JSON (stored in `Report.json`)

```json customize kind=report
{"operation": "create", "report_name": "Unpaid Sales Invoices (AI example)", "ref_doctype": "Sales Invoice",
 "report_type": "Report Builder", "purpose": "Submitted invoices that still have an outstanding amount",
 "builder": {
   "fields": [["name", "Sales Invoice"], ["customer", "Sales Invoice"], ["posting_date", "Sales Invoice"],
              ["due_date", "Sales Invoice"], ["grand_total", "Sales Invoice"], ["outstanding_amount", "Sales Invoice"]],
   "filters": [["Sales Invoice", "docstatus", "=", 1], ["Sales Invoice", "outstanding_amount", ">", 0]],
   "order_by": "due_date asc",
   "add_totals_row": 1}}
```

| Key | Format |
|---|---|
| `fields` | `[[fieldname, doctype], ...]`; doctype is `ref_doctype` or one of its child tables (`["item_code", "Sales Invoice Item"]`). Standard columns allowed: `name owner creation modified modified_by docstatus idx parent` |
| `filters` | `[[doctype, fieldname, operator, value], ...]`. Operators: `=` `!=` `>` `<` `>=` `<=` `in` `not in` (value a list) `like` `not like` (`%` wildcards) `between` (`["2026-01-01", "2026-12-31"]`) `is` (`"set"` / `"not set"`) `descendants of` `ancestors of` `not descendants of` `not ancestors of`. **Not `Timespan`** (refused here) |
| `order_by` | `"<field> asc|desc"` for a field of `ref_doctype` (comma-separated for several). `` `tab<DocType>`.`<field>` desc `` works only when the DocType name has no space: the checker splits on spaces, so `` `tabSales Invoice`.`due_date` `` is refused. Default: `creation desc` |
| `group_by` | `{"group_by": "`tab<DocType>`.`<field>`", "aggregate_function": "count|sum|avg", "aggregate_on": "<numeric field>"}`: one group, one aggregate. With a group-by Frappe ignores `order_by` and sorts by `_aggregate_column desc`, which v16 `get_list` currently refuses (*You do not have permission to access field: Sales Invoice._aggregate_column*): the preview fails, so build grouped totals as a Query Report |
| `add_totals_row` | 1 = a totals row |

Every field in `fields`, `filters`, `order_by` and `group_by` must exist; the preview runs the report with
`limit 5` as the requester.

```json customize kind=report
{"operation": "create", "report_name": "Invoiced Items (AI example)", "ref_doctype": "Sales Invoice",
 "report_type": "Report Builder", "purpose": "Submitted invoice lines of this year, with the item and quantity",
 "builder": {
   "fields": [["name", "Sales Invoice"], ["customer", "Sales Invoice"], ["posting_date", "Sales Invoice"],
              ["item_code", "Sales Invoice Item"], ["qty", "Sales Invoice Item"], ["base_net_amount", "Sales Invoice Item"]],
   "filters": [["Sales Invoice", "docstatus", "=", 1], ["Sales Invoice", "posting_date", "between", ["2026-01-01", "2026-12-31"]]],
   "order_by": "posting_date desc",
   "add_totals_row": 1}}
```

## 3. Query Report

Rules (checked):
- Exactly **one** read-only statement: `SELECT`, `WITH … SELECT`, or a `UNION`. No `INTO OUTFILE/DUMPFILE/@var`,
  `FOR UPDATE`, `SLEEP`, `BENCHMARK`, `LOAD_FILE`, `GET_LOCK`. No second statement.
- Tables are `` `tab<DocType>` `` of existing DocTypes that **both the requester and the approver can read**; no
  system tables (`__Auth`, `tabSessions`, `tabOAuth…`, `tabAccess Log`, `tabServer Script`, `tabUser Permission`, …),
  no pay data (Salary Slip …), no code/access/settings DocTypes, no other database.
- Parameters are `%(fieldname)s`; **every parameter needs a filter with that exact fieldname** (else `KeyError`),
  and every filter should be used.
- A literal `%` is written `%%` (`like 'INV-%%'`, `date_format(d, '%%Y-%%m')`), because the query runs with values.
- **The query report screen sends only filters that have a value** (`query_report.js get_filter_values`). An empty
  optional filter is missing from the dict, so `%(customer)s` raises `KeyError: b'customer'` even inside
  `ifnull(%(customer)s, '')`. **Make every filter used in the query `mandatory: 1`** (with a `default` for
  convenience); for optional filters build a Report Builder report. **(docs differ: they present filters as plain
  "formatting variables")**
- Column labels: `as "Label:Fieldtype/Options:Width"` (`"Customer:Link/Customer:160"`, `"Amount:Currency:120"`,
  `"Month::90"` for plain text), or send `columns` (their order must match the SELECT order).
- Compare Datetime columns with Date filters through `date(col)` so the last day is included.
- `AND` binds tighter than `OR`: wrap every `OR` in parentheses.
- Large tables (>100k rows) scanned without a date filter are warned.

The preview runs `SET STATEMENT max_statement_time=5 FOR SELECT * FROM (<query>) _q LIMIT 5` with
`sample_filters` (or the default company, the first of this month, today) and masks the rows.

### Monthly net sales by customer group (joins Customer: not possible in the Builder)

```sql query-report ref="Sales Invoice" filters="company:Link/Company from_date:Date to_date:Date"
select
    c.customer_group as "Customer Group:Link/Customer Group:160",
    date_format(si.posting_date, '%%Y-%%m') as "Month::90",
    count(si.name) as "Invoices:Int:90",
    sum(si.base_net_total) as "Net Sales:Currency:140"
from `tabSales Invoice` si
join `tabCustomer` c on c.name = si.customer
where si.docstatus = 1
  and si.company = %(company)s
  and si.posting_date between %(from_date)s and %(to_date)s
group by c.customer_group, date_format(si.posting_date, '%%Y-%%m')
order by 2 desc, 4 desc
```

The request for it:

```json customize kind=report
{"operation": "create", "report_name": "Monthly Sales by Customer Group (AI example)", "ref_doctype": "Sales Invoice",
 "report_type": "Query Report", "purpose": "Net sales per customer group and month",
 "query": "<the SELECT above>",
 "filters": [{"fieldname": "company", "label": "الشركة", "fieldtype": "Link", "options": "Company", "mandatory": 1},
             {"fieldname": "from_date", "label": "من تاريخ", "fieldtype": "Date", "mandatory": 1},
             {"fieldname": "to_date", "label": "إلى تاريخ", "fieldtype": "Date", "mandatory": 1, "default": "Today"}],
 "roles": ["Accounts Manager", "Sales Manager"]}
```

### Items sold per item group (child table + parent + Item)

```sql query-report ref="Sales Invoice" filters="company:Link/Company from_date:Date to_date:Date"
select
    i.item_group as "Item Group:Link/Item Group:160",
    sii.item_code as "Item:Link/Item:180",
    sum(sii.stock_qty) as "Qty:Float:90",
    sum(sii.base_net_amount) as "Net Amount:Currency:130"
from `tabSales Invoice Item` sii
join `tabSales Invoice` si on si.name = sii.parent
join `tabItem` i on i.name = sii.item_code
where si.docstatus = 1
  and si.company = %(company)s
  and si.posting_date between %(from_date)s and %(to_date)s
group by i.item_group, sii.item_code
order by 4 desc
```

### `filters` and `columns`

| `filters[]` key | Meaning |
|---|---|
| `fieldname` | the `%(fieldname)s` it feeds (snake_case) |
| `label` | shown in the filter bar (user's language) |
| `fieldtype` | Check, Currency, Data, Date, Datetime, Dynamic Link, Float, Int, Link, Select, Time |
| `options` | Link: the DocType; Select: one option per line |
| `mandatory` | 1 = the report does not run without it (use it for every filter in the query) |
| `default` | e.g. `Today` for a Date |

`columns[]`: `fieldname` (snake_case, no spaces), `label`, `fieldtype` (also `Duration`), `options`, `width`.

## 4. Roles: who may run it

- Frappe's default (`Report.set_doctype_roles`) is every role with level-0 permission on `ref_doctype`.
- A Query Report ignores row permissions, so the checker warns for every role that would see rows it cannot see in
  the list (no read on a joined table, only `if_owner`, or User Permissions on the DocType). Without `roles`, this
  tool defaults to the roles that can read **every** table used. Name a narrower audience explicitly.
- Saving a Query Report or a Script Report needs **Script Manager** (`Report.validate` → `frappe.only_for("Script
  Manager")` for every non-standard report that is not Report Builder). **(docs differ: they say "only a System Manager can create
  query reports")**

## 5. Undo

create → the report is disabled (kept for review); update → the previous version (query, builder, `report_script`,
`javascript`, filters, columns, roles); disable ↔ enable.

## 6. Script Report

A non-standard Script Report (`is_standard: "No"`) keeps Python in `report_script` and, optionally, its filters as
JavaScript in `javascript`. Frappe runs the Python with `safe_exec` (`Report.execute_script`) every time someone
opens the report, **as that person**, with the locals `filters` (the filters that have a value), `data` and
`result`: set `data = [columns, rows]` (optionally `[columns, rows, message, chart, report_summary]`), or set
`result = rows` and send `columns` (the Report's columns table). A top-level `return` does **not** work.
**(docs differ: the Script Report page shows `return frappe.db.get_all(...)` and says only Administrators in
developer mode can create them; in v16 a non-standard one needs Script Manager, and `return` is a syntax error)**

### When to pick it

| The user wants | Why a Script Report |
|---|---|
| a **multi-select** filter (several companies, warehouses, customers), a Link filter limited by another filter (`get_query`), a date range | only the `javascript` filters can do it: the `filters` table of Builder and Query reports has no MultiSelectList, no `get_query` |
| a report that **respects permissions** across a join or a total | `frappe.get_list` applies the viewer's role permissions *and* User Permissions; a Query Report reads raw SQL for everyone |
| **computed columns** (a full name from another DocType, a ratio, a status from several fields), optional filters | plain Python over the rows; an empty filter is simply missing from `filters` (no `KeyError`) |
| one DocType's rows, sorted and filtered | not this: a **Report Builder** report |

### The language

The same rules as server scripts (`server-scripts.md` §4–5): no `import`, no top-level `return`, no `.format()`, no
`a, b = ...`, no `frappe.db.sql` / `frappe.qb`, no network, mail or queues. **Read with `frappe.get_list`** (it applies
the viewer's permissions); `frappe.get_all`, `frappe.db.get_value` and `frappe.get_doc` see every record and are
warned. `frappe.get_list` is also the one call allowed on Company, Fiscal Year and Accounting Period (read only,
permission-checked). Aggregates use the dict form: `{"SUM": "base_net_total", "as": "net_sales"}`, `{"COUNT": "*", "as": "n"}`.

### Filters (`javascript`)

```
frappe.query_reports["<exactly the report_name>"] = { filters: [ {fieldname, label, fieldtype, options, default, reqd, get_data, get_query, on_change}, ... ] };
```

- `fieldtype`: `Link`, `MultiSelectList` (value = a list), `Select`, `Date`, `DateRange` (value = [from, to]), `Check`,
  `Data`, `Int`, `Float`, `Currency`, `Autocomplete`, `Dynamic Link`.
- `MultiSelectList` with `get_data: (txt) => frappe.db.get_link_options("<DocType>", txt)` lists only what the viewer
  may read. **Frappe checks a Link filter's value against the viewer's permissions (`validate_filters_permissions`)
  but not a MultiSelectList's values**: the script must keep only the allowed ones (the checker warns otherwise).
- With `javascript`, do not send `filters` (the table is ignored when the script declares filters; the tool refuses
  both). Without `javascript`, the `filters` table works as in a Query Report.
- `__("Label")` translates labels; `frappe.datetime.month_start()` / `frappe.datetime.get_today()` for date defaults;
  `frappe.defaults.get_user_default("Company")` for the user's company.

### Worked example: sales per company, multi-select Company, full names, company permissions respected

The owner's case: choose **several companies**, see each company's **full name**, and never see a company the user
has no permission for — even if they type it into the filter.

The Python (`report_script`):

```python script-report
allowed = frappe.get_list("Company", fields=["name", "company_name"], order_by="name asc")
full_name = {c.name: c.company_name or c.name for c in allowed}

chosen = filters.get("company") or []
if isinstance(chosen, str):
    chosen = [chosen]
companies = [c for c in chosen if c in full_name] if chosen else list(full_name)

columns = [
    {"fieldname": "company", "label": _("Company"), "fieldtype": "Link", "options": "Company", "width": 200},
    {"fieldname": "company_name", "label": _("Company Name"), "fieldtype": "Data", "width": 260},
    {"fieldname": "invoices", "label": _("Invoices"), "fieldtype": "Int", "width": 100},
    {"fieldname": "net_sales", "label": _("Net Sales"), "fieldtype": "Currency", "width": 160},
]

rows = []
if companies:
    conditions = {"docstatus": 1, "company": ["in", companies]}
    if filters.get("from_date") and filters.get("to_date"):
        conditions["posting_date"] = ["between", [filters.get("from_date"), filters.get("to_date")]]
    totals = frappe.get_list(
        "Sales Invoice",
        filters=conditions,
        fields=["company", {"COUNT": "*", "as": "invoices"}, {"SUM": "base_net_total", "as": "net_sales"}],
        group_by="company",
        order_by="company asc",
    )
    for t in totals:
        rows.append({"company": t.company, "company_name": full_name.get(t.company), "invoices": t.invoices, "net_sales": t.net_sales})

data = [columns, rows]
```

The filters (`javascript`; the name inside must equal `report_name`):

```js
frappe.query_reports["Sales per Company (AI example)"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "MultiSelectList",
			options: "Company",
			get_data: function (txt) {
				return frappe.db.get_link_options("Company", txt);
			},
		},
		{ fieldname: "from_date", label: __("From Date"), fieldtype: "Date", default: frappe.datetime.month_start(), reqd: 1 },
		{ fieldname: "to_date", label: __("To Date"), fieldtype: "Date", default: frappe.datetime.get_today(), reqd: 1 },
	],
};
```

The request:

```
{"operation": "create", "report_name": "Sales per Company (AI example)", "ref_doctype": "Sales Invoice",
 "report_type": "Script Report", "purpose": "Net sales per chosen company, full names, only permitted companies",
 "report_script": "<the Python above>", "javascript": "<the JavaScript above>",
 "roles": ["Accounts Manager", "Accounts User"],
 "sample_filters": {"company": ["<a company>"], "from_date": "2026-01-01", "to_date": "2026-12-31"}}
```

Why it respects company permissions three times: the filter's `get_data` lists only companies the viewer may read;
the script drops any chosen company not in `frappe.get_list("Company")` (MultiSelectList values are not checked by
Frappe); and `frappe.get_list("Sales Invoice")` applies the viewer's User Permissions on `company` itself. Frappe
then also drops rows whose Link columns the viewer's User Permissions exclude (`query_report.get_filtered_data`).
The full name comes from `company_name`; an empty filter means "every company I may see".

### Reusing a standard report: `frappe.call("frappe.desk.query_report.run", ...)`

Do not rewrite a report ERPNext already has (Trial Balance, General Ledger, Accounts Receivable…). Call it and
reshape its rows. `frappe.desk.query_report.run` is whitelisted, runs as the viewer and applies the report's own
roles and permissions. Verified on Frappe v16 inside `safe_exec`:

```python
companies = filters.get("company") or []
if isinstance(companies, str):
    companies = [companies]
allowed = frappe.get_list("Company", pluck="name")
companies = [c for c in companies if c in allowed] or allowed

rows = []
for company in companies:
    r = frappe.call(
        "frappe.desk.query_report.run",
        report_name="Trial Balance",
        filters={"company": company, "fiscal_year": filters.get("fiscal_year"),
                 "from_date": filters.get("from_date"), "to_date": filters.get("to_date")},
        ignore_prepared_report=True,
    )
    for row in r.get("result") or []:
        if isinstance(row, dict) and row.get("account"):
            line = dict(row)
            line["company"] = company
            rows.append(line)

columns = [
    {"fieldname": "company", "label": _("Company"), "fieldtype": "Link", "options": "Company", "width": 160},
    {"fieldname": "account", "label": _("Account"), "fieldtype": "Link", "options": "Account", "width": 260},
    {"fieldname": "opening_debit", "label": _("Opening (Dr)"), "fieldtype": "Currency", "width": 130},
    {"fieldname": "opening_credit", "label": _("Opening (Cr)"), "fieldtype": "Currency", "width": 130},
    {"fieldname": "debit", "label": _("Debit"), "fieldtype": "Currency", "width": 130},
    {"fieldname": "credit", "label": _("Credit"), "fieldtype": "Currency", "width": 130},
    {"fieldname": "closing_debit", "label": _("Closing (Dr)"), "fieldtype": "Currency", "width": 130},
    {"fieldname": "closing_credit", "label": _("Closing (Cr)"), "fieldtype": "Currency", "width": 130},
]
data = [columns, rows]
```

- `r` is `{"result": [...], "columns": [...], ...}`; rows of most ERPNext reports are dicts, some are lists —
  check `isinstance(row, dict)`. Total/blank rows have no key field: skip them.
- Pass every filter the called report marks `reqd` (Trial Balance needs `company`, `fiscal_year`, `from_date`,
  `to_date`), or it throws. `ignore_prepared_report=True` returns rows instead of queueing a prepared report.
- The method name must be literal text; a computed name is refused (`py.call_dynamic`).

### Checks it will hit

| Code | Level | Fix |
|---|---|---|
| `roles.missing` | critical | requester and approver need System Manager **and Script Manager** |
| `script.server_scripts_disabled` | critical | `server_script_enabled` in common_site_config (a Script Report runs through `safe_exec`) |
| `py.*` | as for server scripts | see `server-scripts.md` §4–5 |
| `py.call_missing` / `py.call_dynamic` | critical | `frappe.call` to a method that is not whitelisted, or a computed name |
| `py.report_output` | critical | set `data = [columns, rows]` (or `result` plus `columns`) |
| `report.js_target` | critical | `frappe.query_reports["<report_name>"] = {...}` with the exact name |
| `js.*` | critical | JavaScript syntax, no `eval`, no network, no `localStorage` |
| `report.multiselect_permission` | warning | intersect the chosen values with `frappe.get_list("<DocType>", pluck="name")` |
| `report.filter_undeclared` | warning | the script reads a filter the javascript does not declare |
| `py.unchecked_read` | warning | `frappe.get_all`/`get_value`/`get_doc` see every record: use `frappe.get_list` |
| `report.preview` | ok / critical | the script ran on the sample filters as the requester (first 5 rows shown); critical on any exception |
| `report.preview_throw` | warning | `frappe.throw` on the sample filters: send `sample_filters` that make sense |
| `report.output` / `report.columns` | critical | `data` must be `[columns, rows]`; `result` alone needs `columns` |

The preview runs the script once on `sample_filters` (or defaults: the default company — as a list for a
MultiSelectList —, the first of this month, today) inside a rolled-back savepoint, with writes, mail and network
stubbed, and masks the rows.

## 7. Mistakes seen on real sites (acc.dev "تقرير بصمات الموظفين", three versions)

```sql
-- wrong: AND binds tighter than OR, so the second half matches rows of any date
WHERE ec.time BETWEEN %(from_date)s AND %(to_date)s
  AND IFNULL(%(employee)s, '') = '' OR ec.employee = %(employee)s
-- right: the Datetime column compared by date, every filter mandatory
WHERE date(ec.time) BETWEEN %(from_date)s AND %(to_date)s
  AND ec.employee = %(employee)s
```

| Seen | Fix |
|---|---|
| `KeyError: b'from_date'` | every `%(x)s` needs a filter `x`, and it must be `mandatory` |
| `(%(employee)s = '' OR …)` with an optional employee filter → KeyError when left empty | mandatory, or a Builder report |
| `ec.time BETWEEN from AND to` dropped the last day | `date(ec.time) BETWEEN …` |
| column fieldnames `"Employee Name"`, `"Check-ins Count"` | snake_case `employee_name`, `checkins_count` |
| invented keys `json_options`, `add_total_row` at the top level | only the arguments above; the Builder uses `add_totals_row` inside `builder` |
| `Report 'Sales Invoice' not found` (AI Run) | a DocType is not a report: query it, or create a Builder report |
| `Fiscal Year … required`, `Company is mandatory` when running standard reports | pass the report's mandatory filters (`reports.describe`) |

Sources: https://docs.frappe.io/framework/user/en/desk/reports/report-builder ,
https://docs.frappe.io/framework/user/en/desk/reports/query-report ,
https://docs.frappe.io/framework/user/en/desk/reports/script-report — verified against the Frappe 16.28 source.
