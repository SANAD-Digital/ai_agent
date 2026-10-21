# Number cards and charts (customize.request_dashboard) — Frappe v16

Verified against `frappe/desk/doctype/number_card/number_card.py`, `desk/doctype/dashboard_chart/dashboard_chart.py`
(+ their `.js` forms), `utils/dashboard.py` and `public/js/frappe/widgets/number_card_widget.js` (Frappe 16.28).
Every example passes `customize.check` (kind `dashboard`) on a live site.

Arguments: `operation` (`create` | `update` | `delete`), `object` (`number_card` | `chart`), `name`, `definition`
(Frappe field names, below), `workspace` (where to place it), `purpose`. Cards and charts have **no disabled flag**:
`delete` removes one, and its undo recreates it with its workspace placements. `is_standard` is always 0 and
`module` empty (a standard card or chart is exported into its app in developer mode).

## 1. When to use which

| Need | Use |
|---|---|
| One number on a workspace: "open orders", "unpaid total", "average invoice" | `object: number_card`, `type: Document Type` |
| A number that only a report computes (a total column of a report) | `number_card`, `type: Report` |
| A trend over time (per month / week / day) | `chart`, `chart_type: Count` / `Sum` / `Average` |
| A split by status, customer group, owner… | `chart`, `chart_type: Group By` (`type: Donut` / `Bar` / `Percentage`) |
| A chart of a report's rows | `chart`, `chart_type: Report` |
| A whole new workspace, or a change to a workspace's layout | `customize.request_workspace` (see workspaces.md) |

Custom cards (`method`) and custom charts (`source`) run code: not offered.

## 2. Number card `definition`

| Key | Meaning |
|---|---|
| `type` | `Document Type` (default) or `Report` |
| `document_type`, `parent_document_type` | the DocType; a child table needs its parent (`Sales Invoice Item` + `Sales Invoice`) |
| `function` | `Count` (default) `Sum` `Average` `Minimum` `Maximum` |
| `aggregate_function_based_on` | the numeric field for Sum/Average/Minimum/Maximum (Int, Float, Currency, Percent, Duration) |
| `filters` | `[[fieldname, operator, value]]`, `[[doctype, fieldname, operator, value]]` or `{fieldname: value}` |
| `report_name`, `report_field`, `report_function` | Report cards: the report, its numeric column, `Sum` / `Average` / `Minimum` / `Maximum`; `filters` is then an object of report filter values |
| `label`, `color`, `currency`, `is_public`, `show_percentage_stats`, `stats_time_interval`, `show_full_number` | display |

```json customize kind=dashboard
{"operation": "create", "object": "number_card", "name": "Open ToDos (AI example)",
 "definition": {"document_type": "ToDo", "function": "Count", "filters": [["status", "=", "Open"]]},
 "purpose": "How many tasks are still open"}
```

Add `"workspace": {"name": "<an existing non-app workspace>", "label": "Open ToDos", "col": 3}` to show it there
in the same request.

Operators: `=` `!=` `>` `<` `>=` `<=` `in` / `not in` (a list) `like` / `not like` (`%`) `between` (`[from, to]`)
`is` (`"set"` / `"not set"`), `Timespan` with one of `"today"`, `"yesterday"`, `"tomorrow"`,
`"this week"`, `"this month"`, `"this quarter"`, `"this year"`, `"last week"`, `"last month"`, `"last quarter"`,
`"last 6 months"`, `"last year"`, `"next week|month|quarter|6 months|year"` (Frappe's `TimespanOptions`; "last 7 days"
is not one), `descendants of` / `ancestors of`. Submitted documents only:
`["docstatus", "=", 1]`.

## 3. Chart `definition`

| chart_type | Required | Notes |
|---|---|---|
| `Count` / `Sum` / `Average` | `document_type`, `based_on` (a Date/Datetime field or `creation`), Sum/Average: `value_based_on` (numeric) | `timespan` `Last Year` (default) `Last Quarter` `Last Month` `Last Week` `Select Date Range` (+ `from_date`, `to_date`); `time_interval` `Yearly` `Quarterly` `Monthly` (default) `Weekly` `Daily` |
| `Group By` | `document_type`, `group_by_based_on` (a Link or Select field, or `owner`) | `group_by_type` `Count` (default) / `Sum` / `Average` (+ `aggregate_function_based_on`), `number_of_groups` |
| `Report` | `report_name`, `x_field` and `y_axis` (column fieldnames), or `use_report_chart: 1` | `filters` = report filter values |

`type` (the drawing): `Line` (default), `Bar`, `Percentage`, `Pie`, `Donut`, `Heatmap`. `roles`: who may open the
chart besides those who read the DocType. `is_public`: offered to everyone in the workspace editor.

```json customize kind=dashboard
{"operation": "create", "object": "chart", "name": "ToDos per Month (AI example)",
 "definition": {"chart_type": "Count", "document_type": "ToDo", "based_on": "creation",
   "timespan": "Last Year", "time_interval": "Monthly", "type": "Bar"},
 "purpose": "Tasks created each month"}
```

```json customize kind=dashboard
{"operation": "create", "object": "chart", "name": "ToDos by Status (AI example)",
 "definition": {"chart_type": "Group By", "document_type": "ToDo", "group_by_based_on": "status", "type": "Donut"},
 "purpose": "Share of tasks per status"}
```

Update sends only what changes (`{"operation": "update", "object": "chart", "name": "…", "definition":
{"time_interval": "Weekly"}}`); read the current definition first with `customize.get`.

## 4. Placing it on a workspace

`workspace`: a name, `{name, label, col}` (col 3, 4, 6, 8 or 12; cards default 4, charts 12) or a list. The widget is
added to the workspace's table and a block is inserted after the last block of the same kind. Only plain (non-app)
workspaces: for an app workspace (Selling, Accounting…) first copy it with `customize.request_workspace`
`copy_from`. Placing on a public workspace needs Workspace Manager as well.

## 5. Checks and how to fix them

| Code | Level | Fix |
|---|---|---|
| `dashboard.name_taken` | critical | another name, or `update` |
| `dashboard.standard` | critical | app-owned card/chart: create a copy under another name |
| `dashboard.doctype` / `dashboard.parent` | critical | an existing, non-Single DocType; not code/access/settings DocTypes; a child table needs `parent_document_type` |
| `dashboard.read` | critical | the requester must read the DocType |
| `dashboard.field` | critical | aggregates are numeric, `based_on` is Date/Datetime, `group_by_based_on` exists |
| `dashboard.filter` | critical | the field exists on the DocType (or its child table), a listed operator, the right value shape |
| `dashboard.function` / `dashboard.report` | critical | Report cards need `report_name`, `report_field`, `report_function`; the report exists, is enabled, the requester may run it |
| `dashboard.timespan` | critical / warning | `Select Date Range` needs both dates; an interval the form does not offer for that timespan is a warning |
| `dashboard.shape` | critical / warning | Heatmap only for Count/Sum/Average; a pie of a time series is a warning |
| `dashboard.workspace` | critical | the workspace exists, is not app-owned, the label is free on it |
| `dashboard.in_use` | critical | delete: shown on an app workspace or a Dashboard |
| `dashboard.frappe` | critical | Frappe's own validation failed in the dry run: the message says why |
| `dashboard.preview` | critical | the real computation failed (as the requester): fix the filters/fields it names |
| `dashboard.public_roles` | warning | `is_public` on a DocType some roles cannot read: Frappe refuses them the card; drop `is_public` if not needed |
| `dashboard.role_widening` | warning | chart `roles` that cannot read the DocType **see the numbers** (chart data is cached per chart): remove them |
| `dashboard.cache_shared` | warning | the DocType is limited per user (if_owner / User Permissions) and chart data is cached per chart: a viewer can see someone else's totals; prefer a number card |
| `dashboard.heatmap` | warning | Heatmap counts with `get_all`, without permissions |
| `dashboard.query_report` | warning | a Query Report source reads every row |
| `dashboard.sensitive` | warning | pay or personal data: the preview is masked |

The preview shows the real value (card) or the last 24 points (chart), computed as the requester.
