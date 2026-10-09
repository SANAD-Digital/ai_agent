---
name: inventory
title: المخزون
description: Load for any stock question: current quantity or value by item/warehouse/group, stock on a past date, item movement history, consumption, slow-moving or dead stock, stock ageing, items below re-order level, negative stock, warehouses, and diagnosis of why stock did not move for a document or why an item does not appear in item search.
tools: metrics.query, data.query, reports.run, reports.describe, erp.stock_balance, erp.item_details, diagnose.document, knowledge.search, artifacts.create_table, artifacts.create_chart
---

# Inventory

## Fast paths (no schema discovery needed)

| Question | Call |
|---|---|
| Stock qty now | `metrics.query {metric:"stock_qty", dimensions:["item"], filters:{warehouse:"Stores - X"}}`; dims: item, item_name, item_group, warehouse |
| Stock value now | `metrics.query {metric:"stock_value", dimensions:["warehouse"]}`; dims: item, item_group, warehouse |
| Items below re-order level | `metrics.query {metric:"items_below_reorder"}` |
| One item in one warehouse, on a date | `erp.stock_balance {item_code, warehouse, date}` (qty, valuation_rate, value) |
| Price/UOM/default warehouse for a transaction | `erp.item_details {item_code, company, doctype:"Sales Invoice"}` |
| Item found by name (Arabic-safe) | `data.query {doctype:"Item", fields:["name","item_name","stock_uom","disabled"], filters:[{field:"item_name",operator:"like_normalized",value:"%text%"}], limit:20}` |
| Item by barcode | `data.query {doctype:"Item Barcode", parent_doctype:"Item", fields:["parent","barcode"], filters:[{field:"barcode",operator:"=",value:"..."}]}` |

Stock metrics are current (Bin), not historical; for a past date use `erp.stock_balance` or the Stock Ledger Entry shapes below. Filter a company with `company` (it maps to `warehouse.company`).

## `data.query` shapes

Cost guard: child tables and any DocType over 10k rows are rejected without a date filter (or `parent.<date>`), a `parent` filter, or two `=`/`in` filters on Link/Select fields. Always send a date range.

Current stock with reservations (Bin): fields `item_code, warehouse, actual_qty, reserved_qty, ordered_qty, indented_qty, projected_qty, valuation_rate, stock_value`; filters `item_code` / `warehouse`. Negative stock: `actual_qty < 0`. Available to promise = `projected_qty`.

Movement history (Stock Ledger Entry):
```
data.query {doctype:"Stock Ledger Entry",
  fields:["posting_date","voucher_type","voucher_no","warehouse","actual_qty","qty_after_transaction","valuation_rate","stock_value_difference"],
  filters:[{field:"item_code",operator:"=",value:"ITEM"},{field:"is_cancelled",operator:"=",value:0},{field:"posting_date",operator:"between",value:["2026-01-01","2026-03-31"]}],
  order_by:[{field:"posting_datetime",direction:"desc"}], limit:50}
```
Qty on a past date = `qty_after_transaction` of the last row with `posting_datetime` ≤ that date, per warehouse.

Consumption / outflow by item: SLE fields `item_code, {field:"actual_qty",aggregate:"SUM",alias:"qty_out"}`, filters `actual_qty < 0`, `is_cancelled = 0`, date range, group_by `["item_code"]`. Split by `voucher_type` (Delivery Note, Sales Invoice, Stock Entry, Purchase Receipt, Stock Reconciliation) when the user asks where stock went.

Slow movers: (1) SLE `{field:"posting_date",aggregate:"MAX",alias:"last_out"}` with `actual_qty < 0`, `is_cancelled = 0`, `posting_date` within the last 6–12 months, group_by `item_code`; (2) Bin rows with `actual_qty > 0`; items in (2) with no or old `last_out` are slow/dead. Or run Stock Ageing.

Re-order settings: `doctype:"Item Reorder", parent_doctype:"Item"`, fields `parent, warehouse, warehouse_reorder_level, warehouse_reorder_qty, material_request_type`; compare with Bin `projected_qty`.

## Reports (pass every required filter; no defaults are filled)

- **Stock Balance** `{company, from_date*, to_date*, item_group, item_code:[..], warehouse:[..], include_zero_stock_items:0}`: opening, in, out, closing qty and value. Prepared Report on this site: may return "queued"; say so and don't poll. Prefer metrics / Bin for "now".
- **Stock Ledger** `{company*, from_date*, to_date*, item_code:[..], warehouse:[..], voucher_no}`: row-level movements with balance.
- **Stock Ageing** `{company*, to_date*, warehouse, item_code, range:"30, 60, 90", show_warehouse_wise_stock:0}`: qty by age bucket (slow/dead stock).
- **Stock Projected Qty** `{company, warehouse, item_code, item_group}`: actual, ordered, reserved, projected, re-order level, shortage.
- **Item Shortage Report** `{company*, warehouse:[..]}`; **Warehouse Wise Stock Balance** `{company*}`; **Total Stock Summary** `{group_by*: "Warehouse"|"Company", company*}`.
- **Stock and Account Value Comparison** `{company*, as_on_date, account}`: stock ledger vs stock account mismatch.
- **Itemwise Recommended Reorder Level** `{item_group*, from_date, to_date}`.

## Key DocTypes and fields

- **Item**: item_code, item_name, item_group, brand, stock_uom, is_stock_item, is_sales_item, is_purchase_item, is_fixed_asset, disabled, end_of_life, has_variants, variant_of, has_batch_no, has_serial_no, valuation_method, valuation_rate, standard_rate; tables item_defaults (company, default_warehouse), reorder_levels, barcodes.
- **Bin** (one row per item+warehouse): actual_qty, reserved_qty, ordered_qty, indented_qty, projected_qty, valuation_rate, stock_value.
- **Stock Ledger Entry**: item_code, warehouse, posting_date, posting_datetime, voucher_type, voucher_no, actual_qty (+in / −out), qty_after_transaction, incoming_rate, valuation_rate, stock_value_difference, is_cancelled, company, batch_no.
- **Warehouse**: warehouse_name, company, is_group, parent_warehouse, disabled, warehouse_type.
- **Stock Entry**: purpose (Material Issue, Material Receipt, Material Transfer, Manufacture, Repack …), posting_date, from_warehouse, to_warehouse; rows s_warehouse, t_warehouse, qty, transfer_qty, basic_rate.

## Rules

1. Never mix current balances (Bin) with historical movement (SLE) in one figure.
2. Always filter SLE with `is_cancelled = 0` and a date range or item; aggregate instead of pulling full ledgers.
3. Quantities are in `stock_uom`; say "qty" vs "value" explicitly. Value is company currency.
4. Group warehouses (`is_group = 1`) hold no stock; filter leaf warehouses or use `descendants of`.
5. Flag negative stock as an anomaly.
6. Output: `artifacts.create_chart` for trends and comparisons, `artifacts.create_table` for lists over ~10 rows. A report that rejects its filters: `reports.describe` once, then retry.

## Diagnose: stock did not move for a document

1. `diagnose.document {doctype, name}` (Sales/Purchase Invoice, Delivery Note, Purchase Receipt, Stock Entry, Stock Reconciliation). Use the open document when the user says "this invoice".
2. Stop at the first failing check: docstatus (only submitted moves stock) → update_stock (invoice without it moves nothing; a linked Delivery Note / Purchase Receipt carries the movement) → is_stock_item (non-stock items never enter the ledger) → warehouse (stock rows need one) → stock_ledger_entry (none on a submitted document = posting problem).
3. Posting problem: check `Repost Item Valuation` for the voucher (`voucher_no`, status Queued/In Progress/Failed, error_log); suggest the Error Log or a repost by an administrator.
4. A check marked unknown = the user lacks permission; say so. Never change the document; a fix on a draft goes through the normal approval request.

## Diagnose: item not found in item search

1. Name given instead of code: find it with `like_normalized` on `item_name` (or barcode), then `diagnose.document {doctype:"Item", name, company}`.
2. Causes: disabled; end_of_life passed; template with variants (pick the variant); not `is_sales_item` (sales documents) / not `is_purchase_item` (purchase documents); no Item Default for the company; in POS, the POS Profile `item_groups` list excludes its group.
3. Explain the fix as steps in the Item form; use `knowledge.search` for detailed guide steps and cite it.
