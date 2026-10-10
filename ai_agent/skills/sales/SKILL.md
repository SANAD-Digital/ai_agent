---
name: sales
title: Sales
description: Load for any selling, customer or receivables question: sales totals and trends, period comparison, returns, top customers/items/item groups, sales by branch/cost center/territory/sales person/POS/payment mode, open sales orders, unbilled deliveries, quotations, customer activity and dormant customers, collections, customer balances, outstanding and aging.
tools: metrics.query, analysis.sales, data.query, data.get_document, reports.run, reports.describe, erp.invoice_payments, erp.party_details, artifacts.create_table, artifacts.create_chart
---

# Sales, customers and receivables

## Fast paths (no schema discovery needed)

KPI numbers come from `metrics.query` first; it applies permissions and returns period, source and assumption.

| Question | Call |
|---|---|
| Net sales (after returns, before tax) | `metrics.query {metric:"net_sales", period:"this_month"}` |
| Gross sales / returns / return % | metric `gross_sales` / `sales_returns` (positive value) / composite `sales_return_rate` |
| Invoice count / average invoice | `sales_invoice_count` / `avg_invoice_value` |
| Quantity sold | `sold_qty` (dims: item, item_group, customer) |
| Compare with last period / last year | add `compare:"previous_period"` or `"same_period_last_year"` |
| Top customers / items / groups | `metrics.query {metric:"net_sales", dimensions:["customer"], top:10}`; `item`, `item_group` work the same |
| By branch / cost center / territory / rep / POS / payment mode | dims `branch`, `cost_center`, `territory`, `customer_group`, `sales_person`, `pos_profile`, `payment_mode` |
| Daily series | `dimensions:["date"]` |
| Customer balances now | `metrics.query {metric:"receivables", dimensions:["customer"], top:20}` |
| Receivables aging | `metrics.query {metric:"receivables", dimensions:["age"]}` (buckets by due date: not due, 1-30, 31-60, 61-90, >90) |
| Collections | `collections` (dims: mode_of_payment, party, date) |
| One-call dashboard (totals + top customers + share) | `analysis.sales {period, company, top}` |

Metric dims: `net_sales`/`gross_sales`/`sales_returns` accept customer, customer_group, territory, branch, cost_center, pos_profile, date, item, item_group, sales_person, payment_mode; `sales_invoice_count`/`avg_invoice_value` accept the header ones only (no item, item_group, sales_person, payment_mode). Filter with `filters:{customer:"X"}` or `{territory:["A","B"]}`. Add `company` when companies use different currencies.
- `branch` is not a standard Sales Invoice field; if the site has no branch field the engine drops it with a note: then use `cost_center`.
- `analysis.sales` uses `base_grand_total` (tax included, returns excluded); `net_sales` is before tax and after returns. Say which one you report; never mix them.
- `payment_mode` is computed on grand total incl. tax; unpaid part shows as credit (آجل).

## When the catalog does not cover it: `data.query` shapes

Flat arguments; aggregates as `{field, aggregate, alias}`; filters as `{field, operator, value}`.
Cost guard: child tables and any DocType over 10k rows are rejected without a date filter (or `parent.<date>`), a `parent` filter, or two `=`/`in` filters on Link/Select fields. Always send a date range.

Monthly trend:
```
data.query {doctype:"Sales Invoice", fields:[{field:"base_net_total", aggregate:"SUM", alias:"net"}],
  filters:[{field:"docstatus",operator:"=",value:1},{field:"posting_date",operator:"between",value:["2026-01-01","2026-06-30"]},{field:"company",operator:"=",value:"X"}],
  group_by:[{field:"posting_date", bucket:"month", alias:"month"}], order_by:[{field:"month",direction:"asc"}]}
```
Item lines (warehouse, brand, rate questions): `doctype:"Sales Invoice Item", parent_doctype:"Sales Invoice"`; fields `item_code, item_name, item_group, brand, warehouse, qty, stock_qty, base_net_amount, base_net_rate, incoming_rate`; parent fields only in filters, e.g. `{field:"parent.posting_date",operator:"between",...}`, `parent.customer`, `parent.company`, `parent.is_return` (`docstatus` exists on the row). You cannot select or group by parent fields; date buckets need a date on the queried table.

Last purchase date per customer: Sales Invoice, fields `customer`, `{field:"posting_date",aggregate:"MAX",alias:"last_date"}`, group_by `["customer"]`.

## Reports (pass every required filter; no defaults are filled)

- **Accounts Receivable** `{company*, report_date, party_type:"Customer", party:["CUST"], ageing_based_on:"Due Date", range:"30, 60, 90, 120"}`; per customer: **Accounts Receivable Summary** (same filters). Includes journal entries and unallocated payments that the `receivables` metric misses. On this site it is a Prepared Report: it may return "queued" — say so, don't poll.
- **Customer Ledger Summary** `{company*, from_date*, to_date*, party:"CUST"}`: opening, invoiced, paid, returns, closing per customer.
- **Sales Analytics** `{tree_type*: Customer|Customer Group|Item|Item Group|Territory|Order Type|Project, doc_type*: "Sales Invoice"|"Sales Order"|"Delivery Note"|"Quotation", value_quantity*: "Value"|"Quantity", from_date*, to_date*, company*, range*: Weekly|Monthly|Quarterly|Yearly}`: pivot by period.
- **Item-wise Sales Register** `{from_date*, to_date*, company, customer, item_code, item_group, warehouse, group_by: Customer|Item|Item Group|Territory|Invoice}`; **Sales Register** for invoice-level with taxes.
- **Sales Order Analysis** `{company*, from_date*, to_date*, status:["To Deliver and Bill","To Deliver","To Bill"]}`: pending qty/amount, delay days.
- **Inactive Customers** `{days_since_last_order:90, doctype:"Sales Invoice"}`; **Customers Without Any Sales Transactions** (no filters).
- **Customer Acquisition and Loyalty** `{view_type*: Monthly|"Territory Wise", company*, from_date*, to_date*}`: new vs repeat.
- **Sales Person-wise Transaction Summary** `{company*, doc_type:"Sales Invoice", from_date, to_date, sales_person}`.
- **Item-wise Sales History** `{company*, from_date*, to_date*, item_code, customer}`; **Lost Quotations**; **Customer Credit Balance** `{company*, customer}`.
- Profit by item/customer: **Gross Profit** (see accounting skill).

## Key DocTypes and fields

- **Sales Invoice**: customer, customer_name, customer_group, territory, posting_date, due_date, company, cost_center, project, pos_profile, is_pos, is_return, return_against, update_stock, base_net_total, base_grand_total, outstanding_amount, status (Unpaid, Overdue, Partly Paid, Paid, Return, Credit Note Issued), debit_to.
- **Sales Order**: customer, transaction_date, delivery_date, status (To Deliver and Bill, To Deliver, To Bill, Completed, Closed, On Hold), per_delivered, per_billed, base_net_total, advance_paid. Item rows: qty, delivered_qty, billed_amt.
- **Delivery Note**: posting_date, status ("To Bill" = delivered not invoiced), per_billed, is_return.
- **Quotation**: quotation_to, party_name, transaction_date, valid_till, status (Open, Replied, Partially Ordered, Ordered, Lost, Expired), order_lost_reason.
- **Customer**: customer_name, customer_group, territory, customer_type, disabled, is_frozen, default_currency, payment_terms, credit_limits (table).
- **Payment Entry**: payment_type "Receive", party_type "Customer", party, posting_date, mode_of_payment, base_received_amount, unallocated_amount, references (reference_doctype, reference_name, allocated_amount).

## Rules

1. Submitted only: `docstatus = 1`, unless the user asks for drafts.
2. Return invoices (`is_return = 1`) carry negative amounts: summing all submitted invoices already gives net. For gross, filter `is_return = 0`; report returns separately.
3. Amounts: use `base_*` fields (company currency). Never add amounts across companies with different currencies.
4. Outstanding ≠ grand total. Credit notes have negative outstanding.
5. "Who paid this invoice": `erp.invoice_payments {doctype:"Sales Invoice", name}`. Customer terms/account/price list: `erp.party_details {party_type:"Customer", party}`.
6. State period, company and basis (net/gross, tax incl./excl.) in one line. For change questions, show both periods and the top contributors to the difference; no causal claims beyond the data.
7. Open a specific document with `data.get_document {doctype, name}`.
8. Output: `artifacts.create_chart` for trends and comparisons, `artifacts.create_table` for lists over ~10 rows. A report that rejects its filters: `reports.describe` once, then retry.
9. Customer phone/email only when the user can read them; the gateway already redacts restricted fields.
