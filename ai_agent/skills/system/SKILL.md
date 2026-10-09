---
name: system
title: النظام والإعداد
description: Load for system-level questions and as the fallback when no domain skill (sales, purchasing, inventory, accounting, documents) covers the data: is ERPNext set up / ready for an area, what is missing in company/accounts/stock/selling/buying settings, data quality audits (duplicates, missing fields, negative stock, future dates, orphan links), finding unknown DocTypes, fields, links or workflows, and finding, describing and running any standard or custom report.
tools: setup.check, knowledge.search, schema.search_doctypes, schema.describe_doctype, schema.describe_links, schema.describe_workflow, skills.read_reference, reports.search, reports.describe, reports.run, data.query, data.count, artifacts.create_table
---

# System, setup and discovery

## Setup readiness

`setup.check {area, company}` reads the live values; areas: company, accounts, stock, selling, buying (all five for "is the system ready"). It compares against:
- company: default currency, country, default cost center, a fiscal year covering today, at least one warehouse.
- accounts: default receivable, payable, income, expense, round-off, exchange gain/loss and cash accounts on the company; every Mode of Payment linked to an account for the company; frozen-accounts date noted.
- stock: valuation method and default UOM in Stock Settings; with perpetual inventory, the inventory, stock adjustment and stock-received-not-billed accounts on the company; negative stock flagged; at least one stock item.
- selling: default customer group, territory and selling price list in Selling Settings; an enabled selling price list.
- buying: default supplier group and buying price list in Buying Settings; at least one enabled supplier.

Report only what is missing or risky, grouped by area, each with its steps; "unknown" items are permission limits. Read only: settings are changed by an administrator in the desk. Use `knowledge.search` for detailed guide steps and cite source and page.

## Schema discovery (fallback only)

1. Common DocTypes have prepared field summaries; read them instead of describing: `skills.read_reference {skill_name:"system", reference:"sales-invoice"}` (DocType name lower-case with dashes). Available: bin, customer, delivery-note, gl-entry, item, journal-entry, journal-entry-account, payment-entry, purchase-invoice, purchase-invoice-item, sales-invoice, sales-invoice-item, sales-order, stock-ledger-entry, supplier. Site custom fields are not in them.
2. Anything else: `schema.search_doctypes {query}` → `schema.describe_doctype {doctype, fields_filter:["term"]}` (keep the filter; full dumps are expensive) → `schema.describe_links` for relations → `schema.describe_workflow` for states and actions.
3. Never assume a fieldname you have not seen in a skill, a reference or metadata. Child tables: query the child DocType with `parent_doctype`; parent fields can be used in filters (`parent.posting_date`) but not selected or grouped.
4. Cost guard: child tables and DocTypes over 10k rows need a date filter, a `parent` filter, or two `=`/`in` filters on Link/Select fields, or the query is rejected.

## Reports

1. Find: `reports.search {query}`. Describe before running anything not listed in a domain skill: `reports.describe {report_name}`. Script Reports often declare filters only in client JS, so an empty filter list means "unknown", not "none".
2. `reports.run` fills no defaults: pass `company`, dates and every required filter yourself. Multi-select filters (party, account, warehouse, cost_center) take lists.
3. Empty result → the response lists `declared_filters`; fix the filter names once and retry, then conclude.
4. Prepared Reports (e.g. Stock Balance, Accounts Receivable on this site) may answer "queued/pending": tell the user and stop; don't poll in the same run.
5. Keep `sample_limit` low; set `include_raw_rows` only when the user needs row-level detail.
6. Don't run reports the user's roles cannot open (Administrator-only reports for regular users).

Output for report requests:
- Return only the sections asked for; no title, source, notes, totals or commentary unless asked. "Filters plus table only" → filter lines and the table, nothing after it.
- No prefaces, progress narration or calculation commentary.
- Plain Markdown tables; never Markdown inside raw HTML tags (the UI must render and print it).
- Merged companies → one aggregate, no per-company sections. "Not monthly" → no month columns or notes.

## Data quality audit (read only; fixes go through the documents skill with approval)

| Check | Shape |
|---|---|
| Duplicate names | `data.query {doctype:"Customer", fields:["customer_name",{field:"name",aggregate:"COUNT",alias:"n"}], group_by:["customer_name"], order_by:[{field:"n",direction:"desc"}], limit:50}`; keep rows with n > 1 (no HAVING). Same for Supplier `supplier_name`, Item `item_name`. Use `like_normalized` to catch Arabic spelling variants of one name. |
| Missing values | `data.count {doctype, filters:[{field:"tax_id",operator:"is not set"}]}`, then list a sample with `data.query`. Typical: Customer tax_id / mobile_no / customer_group, Supplier supplier_group, Item item_group / Item Default. |
| Negative stock | Bin `actual_qty < 0` (fields item_code, warehouse, actual_qty). |
| Future-dated documents | `posting_date > today`, `docstatus = 1` on Sales/Purchase Invoice, Payment Entry, Journal Entry. |
| Stale drafts | `docstatus = 0` and `modified` older than N days, grouped by `owner`. |
| Disabled masters still in use | Item `disabled = 1` with Bin `actual_qty > 0`; Customer `disabled = 1` with outstanding invoices. |
| Orphan links | collect distinct link values, then `data.count` the target with `name in [...]`; report values that do not exist. |
| Unallocated payments | Payment Entry `docstatus = 1`, `unallocated_amount > 0`. |

State DocType, filters and criteria used; give an actionable recommendation per finding; use `artifacts.create_table` for long lists.
