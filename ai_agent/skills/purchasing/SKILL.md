---
name: purchasing
title: المشتريات
description: Load for any buying, supplier or payables question: purchase totals and trends, period comparison, purchase returns, top suppliers/items, open purchase orders and pending receipts, material requests to order, received-not-billed, supplier activity and delays, payments to suppliers, supplier balances, what we owe, payables aging and due dates.
tools: metrics.query, data.query, data.get_document, reports.run, reports.describe, erp.invoice_payments, erp.party_details, artifacts.create_table, artifacts.create_chart
---

# Purchasing, suppliers and payables

## Fast paths (no schema discovery needed)

| Question | Call |
|---|---|
| Net purchases (after returns, before tax) | `metrics.query {metric:"net_purchases", period:"this_month"}` |
| Compare periods | add `compare:"previous_period"` or `"same_period_last_year"` |
| Top suppliers / items / supplier groups | `metrics.query {metric:"net_purchases", dimensions:["supplier"], top:10}`; dims: supplier, supplier_group, item, date |
| What we owe now, by supplier | `metrics.query {metric:"payables", dimensions:["supplier"], top:20}` |
| Payables aging | `metrics.query {metric:"payables", dimensions:["age"]}` (not due, 1-30, 31-60, 61-90, >90 by due date) |
| Due soon (cash planning) | `metrics.query {metric:"payables", dimensions:["due_date"], sort:"dimension_asc"}` |
| Payments made | `metrics.query {metric:"payments_made", dimensions:["party"]}` (dims: mode_of_payment, party, date; includes non-supplier payments, filter `party` when needed) |
| Payments against one invoice | `erp.invoice_payments {doctype:"Purchase Invoice", name}` |

No metric for gross purchases or purchase returns: use `data.query` on Purchase Invoice with `is_return` 0 / 1 (returns are negative).

## `data.query` shapes

Cost guard: child tables and any DocType over 10k rows are rejected without a date filter (or `parent.<date>`), a `parent` filter, or two `=`/`in` filters on Link/Select fields. Always send a date range.

Monthly purchases:
```
data.query {doctype:"Purchase Invoice", fields:[{field:"base_net_total", aggregate:"SUM", alias:"net"}],
  filters:[{field:"docstatus",operator:"=",value:1},{field:"posting_date",operator:"between",value:["2026-01-01","2026-06-30"]},{field:"company",operator:"=",value:"X"}],
  group_by:[{field:"posting_date", bucket:"month", alias:"month"}], order_by:[{field:"month",direction:"asc"}]}
```
Item lines / last purchase price: `doctype:"Purchase Invoice Item", parent_doctype:"Purchase Invoice"`, fields `item_code, item_name, item_group, qty, stock_qty, base_net_rate, base_net_amount, warehouse, purchase_order, purchase_receipt, expense_account`; filter parent fields as `parent.supplier`, `parent.posting_date`, `parent.company`, `parent.is_return`. Parent fields cannot be selected or grouped.

Open purchase orders: Purchase Order, filters `docstatus=1`, `status in ["To Receive and Bill","To Receive","To Bill"]`; fields `name, supplier, transaction_date, schedule_date, per_received, per_billed, base_grand_total`. Overdue receipt = `schedule_date < today` and `per_received < 100`.

Received not billed: Purchase Receipt, `docstatus=1`, `status in ["To Bill","Partly Billed"]`.

Requested not ordered: Material Request, `docstatus=1`, `material_request_type="Purchase"`, `status in ["Pending","Partially Ordered"]` (per_ordered < 100).

Supplier dormant: Purchase Invoice grouped by `supplier` with `{field:"posting_date",aggregate:"MAX",alias:"last_date"}`.

## Reports (pass every required filter; no defaults are filled)

- **Accounts Payable** `{company*, report_date, party_type:"Supplier", party:["SUPP"], ageing_based_on:"Due Date"|"Posting Date"|"Supplier Invoice Date", range:"30, 60, 90, 120"}`; per supplier: **Accounts Payable Summary**. Includes journal entries and unallocated advances that the `payables` metric misses.
- **Supplier Ledger Summary** `{company, from_date*, to_date*, party:"SUPP"}`: opening, invoiced, paid, returns, closing.
- **Purchase Analytics** `{tree_type*: Supplier|Supplier Group|Item|Item Group, doc_type*: "Purchase Invoice"|"Purchase Order"|"Purchase Receipt", value_quantity*: Value|Quantity, from_date*, to_date*, company*, range*: Weekly|Monthly|Quarterly|Yearly}`.
- **Purchase Order Analysis** `{company*, from_date*, to_date*, status:["To Receive and Bill","To Receive","To Bill"]}`: pending qty, received, billed, delay.
- **Item-wise Purchase Register** `{from_date*, to_date*, company, supplier, item_code, item_group, group_by: Supplier|Item|Item Group|Invoice}`; **Purchase Register** for invoice-level with taxes.
- **Item-wise Purchase History** `{company*, from_date*, to_date*, item_code, supplier}`; **Procurement Tracker** `{company, from_date, to_date}` (MR → PO → receipt chain); **Supplier Quotation Comparison** for price comparison.

## Key DocTypes and fields

- **Purchase Invoice**: supplier, supplier_name, posting_date, bill_no, bill_date (supplier's number/date), due_date, company, cost_center, is_return, return_against, update_stock, is_paid, on_hold, base_net_total, base_grand_total, outstanding_amount, status (Unpaid, Overdue, Partly Paid, Paid, Return, Debit Note Issued), credit_to.
- **Purchase Order**: supplier, transaction_date, schedule_date, status, per_received, per_billed, base_net_total. Item rows: qty, received_qty, billed_amt, expected_delivery_date, material_request.
- **Purchase Receipt**: posting_date, status, per_billed, is_return. Item rows: received_qty, rejected_qty, purchase_order, purchase_invoice.
- **Material Request**: material_request_type, transaction_date, schedule_date, status, per_ordered, per_received.
- **Supplier**: supplier_name, supplier_group, supplier_type, country, disabled, on_hold, hold_type (All, Invoices, Payments), default_currency, payment_terms.
- **Payment Entry**: payment_type "Pay", party_type "Supplier", party, posting_date, mode_of_payment, base_paid_amount, unallocated_amount, references.

## Rules

1. Submitted only (`docstatus = 1`) unless drafts are asked for.
2. Returns (`is_return = 1`) are negative; summing all submitted invoices gives net. Report returns separately when asked.
3. Use `base_*` fields; never add across companies with different currencies.
4. Outstanding ≠ grand total. A supplier on hold (`on_hold`, `hold_type`) explains blocked invoices or payments.
5. Supplier delivery delay = receipt `posting_date` vs PO `schedule_date`; say it is computed from these dates.
6. Supplier terms/account/currency: `erp.party_details {party_type:"Supplier", party}`.
7. State period, company and basis (net/gross, tax incl./excl.) in one line.
8. Open a specific document with `data.get_document {doctype, name}`.
9. Output: `artifacts.create_chart` for trends and comparisons, `artifacts.create_table` for lists over ~10 rows. A report that rejects its filters: `reports.describe` once, then retry.
