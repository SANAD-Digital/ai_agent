# Custom reports (customize.request_report) — Frappe v16

Arguments: `operation` (`create` | `update` | `disable`), `report_name`, `ref_doctype`, `report_type`
(`Report Builder` | `Query Report`), `builder` (Report Builder), `query` (Query Report), `filters`, `columns`,
`roles`, `sample_filters` (values for the preview run), `purpose`. `is_standard` is always "No" and `module` is the
module of `ref_doctype`. Verified against `frappe/core/doctype/report/report.py`.

## Builder or Query?

**Report Builder first.** It runs through `frappe.get_list(..., user=user)`: every viewer sees only rows they may
read (user permissions, `if_owner`, permission levels), and it needs no Script Manager.

Query Report only when the builder cannot do it: a JOIN between DocTypes that are not parent/child, or an aggregate
the builder's single group-by cannot express. A Query Report runs `frappe.db.sql(query, filters)` directly — **row
permissions are not applied** — and saving it needs Script Manager. The checker says when a query could have been a
Builder report.

## Report Builder: the `builder` JSON (as Frappe stores it in `Report.json`)

```json
{
  "fields": [["name", "Sales Invoice"], ["customer", "Sales Invoice"], ["grand_total", "Sales Invoice"],
             ["item_code", "Sales Invoice Item"]],
  "filters": [["Sales Invoice", "docstatus", "=", 1], ["Sales Invoice", "posting_date", "Timespan", "this month"]],
  "order_by": "`tabSales Invoice`.`posting_date` desc",
  "group_by": {"group_by": "`tabSales Invoice`.`customer`", "aggregate_function": "sum", "aggregate_on": "grand_total"},
  "add_totals_row": 1
}
```

- `fields`: `[fieldname, doctype]`; the doctype is `ref_doctype` or one of its child tables.
- `filters`: `[doctype, fieldname, operator, value]` with the operators of `data.query` (`=`, `!=`, `>`, `<`, `>=`,
  `<=`, `like`, `not like`, `in`, `not in`, `is` set/not set, `between`, `Timespan`).
- `order_by`: `` `tab<DocType>`.`<field>` asc|desc ``. Without it Frappe sorts by `creation desc`.
- `group_by` (optional): `aggregate_function` `count` | `sum` | `avg`, `aggregate_on` a numeric field.
- Every field in `fields`, `filters` and `order_by` must exist; the preview runs the report with `limit 5` as you.

## Query Report

```sql
select
    e.employee as "Employee:Link/Employee:140",
    e.employee_name as "Name::180",
    date(c.time) as "Day:Date:110",
    count(*) as "Punches:Int:80"
from `tabEmployee Checkin` c
join `tabEmployee` e on e.name = c.employee
where date(c.time) between %(from_date)s and %(to_date)s
  and (ifnull(%(employee)s, '') = '' or c.employee = %(employee)s)
group by e.employee, date(c.time)
order by date(c.time) desc
```

- One `SELECT` (or `WITH … SELECT`, or `UNION`). No `INTO OUTFILE/DUMPFILE`, no `SLEEP`, `BENCHMARK`, `LOAD_FILE`,
  `GET_LOCK`.
- Parameters are `%(fieldname)s`. **Every parameter needs a filter with that fieldname, and every filter should be
  used** — a missing one fails at run time with `KeyError: b'from_date'`. For an optional filter give it a `default`
  or write `ifnull(%(x)s, '') = '' or col = %(x)s`.
- A literal `%` in a `like` must be written `%%`.
- Column labels: `as "Label:Fieldtype/Options:Width"` (`"Customer:Link/Customer:160"`, `"Amount:Currency:120"`), or
  send `columns` explicitly; without either the raw column names are shown.
- Tables are `` `tab<DocType>` ``. The requester and the approver must both be able to read every table used.
  System tables and masked DocTypes (`__Auth`, `tabSessions`, OAuth tables, Salary Slip, …) are refused.
- The preview runs `SET STATEMENT max_statement_time=5 FOR SELECT * FROM (<query>) _q LIMIT 5` with
  `sample_filters` (or the default company, the first of the month and today), and the rows shown are masked.
- A full scan of a table over 100k rows without a date filter is warned; set `prepared_report` for heavy reports
  (they then run in the background).
- A Datetime column compared with Date filters: use `date(col) between %(from_date)s and %(to_date)s` so the last
  day is included.

## filters and columns

| filters[] key | Meaning |
|---|---|
| `fieldname` | the `%(fieldname)s` it feeds |
| `label` | shown in the filter bar |
| `fieldtype` | Check, Currency, Data, Date, Datetime, Dynamic Link, Float, Int, Link, Select, Time |
| `options` | Link: the DocType; Select: one option per line |
| `mandatory` | 1 = the report does not run without it |
| `default` | e.g. `Today` for a Date |

`columns[]`: `fieldname`, `label`, `fieldtype`, `options`, `width`.

## Roles: who may run it

- Frappe's default is every role with read (level 0) on `ref_doctype` (`Report.set_doctype_roles`).
- Because a Query Report ignores row permissions, the checker warns when a role would see rows through the report
  that it cannot see in the list (no read on a joined table, only `if_owner`, or user permissions limit it).
  The default here is the roles that can read **every** table used. Give `roles` explicitly when the audience is
  narrower (e.g. `["HR Manager"]`).

## Example — daily check-ins per employee (the request seen on acc.dev)

```json
{"operation": "create", "report_name": "Daily Check-ins", "ref_doctype": "Employee Checkin", "report_type": "Query Report",
 "query": "<the SELECT above>",
 "filters": [{"fieldname": "from_date", "label": "From", "fieldtype": "Date", "mandatory": 1, "default": "Today"},
             {"fieldname": "to_date", "label": "To", "fieldtype": "Date", "mandatory": 1, "default": "Today"},
             {"fieldname": "employee", "label": "Employee", "fieldtype": "Link", "options": "Employee"}],
 "roles": ["HR Manager", "HR User"],
 "sample_filters": {"from_date": "2026-10-01", "to_date": "2026-10-19"},
 "purpose": "Count check-ins per employee and day"}
```

## Undo

create → the report is disabled (kept for review); update → the previous version; disable → enabled again.
