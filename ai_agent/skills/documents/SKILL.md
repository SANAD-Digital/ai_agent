---
name: documents
title: المستندات
description: Load whenever the user wants to create, change, submit, cancel or move a document through workflow: new invoices, orders, quotations, payments, journal entries, items, customers or suppliers; drafts from a file or image; converting a Sales Order to an invoice or delivery note, a Material Request to a purchase order, a Purchase Invoice to a receipt; bulk creation of missing master data; multi-step chains like customer then invoice then payment.
tools: actions.prepare_document, actions.validate_document, actions.request_create_draft, actions.request_bulk_create, actions.request_mapped_draft, actions.request_update, actions.request_submit, actions.request_cancel, actions.request_workflow_action, erp.item_details, erp.party_details, erp.fiscal_year, erp.exchange_rate, erp.stock_balance, erp.invoice_payments, data.query, data.get_document, schema.describe_doctype, schema.describe_workflow, files.read, artifacts.create_document_links
---

# Documents: create, change, submit, workflow

## Safety rules (never relaxed)

1. The agent never saves, submits or cancels directly. Every write is an approval request (`actions.request_*`); the user approves in the chat card. Drafts stay drafts after approval: the agent never submits on its own.
2. Create/Update, Submit, Cancel and workflow actions are separate approvals. Request Submit or Cancel only when the user explicitly asks for it.
3. Cancel is destructive: state what will be reversed (stock, GL, linked payments) before requesting it.
4. Before `actions.request_update`, read the document (`data.get_document`) and pass its `modified` as `expected_modified`; never update a submitted or cancelled document (amend through the desk instead).
5. Never put `ignore_permissions`, `user`, `run_as` or `commit` in a payload. Respect the error when the user lacks permission; don't look for a workaround.
6. Don't invent values. Missing mandatory business data (party, items, qty, price when no price list) → ask one concise question.
7. Present the impact (document, party, totals, key rows) before requesting approval. After approval, return the link with `artifacts.create_document_links`.

## Fast paths

| Request | Call |
|---|---|
| Invoice or delivery note from a Sales Order | `actions.request_mapped_draft {source_doctype:"Sales Order", source_name, target_doctype:"Sales Invoice" or "Delivery Note"}` |
| Purchase Order from a Material Request | `actions.request_mapped_draft {source_doctype:"Material Request", source_name, target_doctype:"Purchase Order"}` |
| Purchase Receipt from a Purchase Invoice | `actions.request_mapped_draft {source_doctype:"Purchase Invoice", source_name, target_doctype:"Purchase Receipt"}` |
| Party defaults (account, currency, price list, terms, taxes) | `erp.party_details {party_type:"Customer" or "Supplier", party, company, doctype}` |
| Row defaults (item_name, uom, rate, accounts, cost center, stock) | `erp.item_details {item_code, company, doctype, customer or supplier, warehouse, qty}` |
| Exchange rate / fiscal year / stock on hand | `erp.exchange_rate`, `erp.fiscal_year`, `erp.stock_balance` |
| Submit / cancel / workflow step | `actions.request_submit` / `actions.request_cancel` / `actions.request_workflow_action {doctype, document_name, action}` |

Mapped drafts use ERPNext's own mapping (prices, taxes, links, pending qty). Never rebuild these documents field by field.

## Building a new draft

1. Find linked records first with `data.query` (`like_normalized` for Arabic names): customer/supplier, items, warehouse, accounts, cost center. Never create a duplicate of something that exists.
2. Call `actions.prepare_document {doctype, payload}` with what you have. It returns `required_fields` and `missing_fields` for this site (custom mandatory fields included), so you rarely need `schema.describe_doctype`; use it with `fields_filter` only for a field you cannot name (e.g. `["naming_series"]` options).
3. Fill header gaps from `erp.party_details`, row gaps from `erp.item_details`. Dates as `YYYY-MM-DD`.
4. `actions.validate_document`; fix and retry once; then `actions.request_create_draft`.

Typical required inputs (this site; prepare confirms):
- Sales Invoice: company, naming_series, customer, posting_date, currency, conversion_rate, selling_price_list, plc_conversion_rate, debit_to, items[item_code, qty, rate, uom, income_account, cost_center]. Set `update_stock:1` + row `warehouse` to move stock; `is_pos` for cash sale; returns: `is_return:1`, `return_against`, negative qty.
- Sales Order: customer, transaction_date, delivery_date, order_type "Sales", company, currency, selling_price_list, items[item_code, qty, rate, delivery_date].
- Quotation: quotation_to "Customer", party_name, transaction_date, order_type, company, currency, selling_price_list, items.
- Purchase Invoice: supplier, posting_date, credit_to, bill_no, bill_date, items[item_code, qty, rate, uom, expense_account]; `update_stock:1` + warehouse to receive stock.
- Purchase Order: supplier, transaction_date, schedule_date, company, currency, items[item_code, qty, rate, schedule_date].
- Material Request: material_request_type (Purchase, Material Transfer, Material Issue, Manufacture, Customer Provided), company, transaction_date, items[item_code, qty, schedule_date, warehouse].
- Payment Entry: payment_type "Receive"/"Pay"/"Internal Transfer", posting_date, company, mode_of_payment, party_type, party, paid_from, paid_to, paid_amount, received_amount, references[reference_doctype, reference_name, allocated_amount] to settle invoices (check outstanding first; `erp.invoice_payments` shows what is already allocated).
- Journal Entry: voucher_type, posting_date, company, accounts[account, debit_in_account_currency | credit_in_account_currency, party_type, party, cost_center]; debits must equal credits.
- Stock Entry: stock_entry_type, company, items[item_code, qty, s_warehouse and/or t_warehouse].
- Customer: customer_name, customer_type (Company/Individual), customer_group, territory. Supplier: supplier_name, supplier_type, supplier_group. Item: item_code, item_name, item_group, stock_uom, is_stock_item.

## Compound and bulk work (many rows, files, chains)

1. Treat the request as one objective. Creating a prerequisite is not completion; keep the target document in context and continue (or offer the next step) after each approval.
2. Read the whole file/image first (`files.read`) and extract every row before preparing. If extraction is incomplete, ask one question instead of guessing.
3. Search all linked records at once; group every truly missing master record (items, suppliers, customers, warehouses, accounts, cost centers, price lists) into ONE `actions.request_bulk_create {documents:[{doctype, payload}], summary}`. Don't prepare each row separately: validate one representative payload, then bulk.
4. Build large child tables in one payload, not one call per row.
5. If validation fails for missing master data, bulk-request those, then resume the target.
6. Chains in order: master data → transaction draft → submit/payment/workflow only when requested.
7. At a stop point, reply briefly: done, pending approvals, what remains, and the next action.

## Existing documents and workflow

1. Read the document first: `docstatus` (0 draft, 1 submitted, 2 cancelled), `status`, `workflow_state`.
2. If the DocType has a workflow, `schema.describe_workflow {doctype}` gives states, allowed actions and roles; use `actions.request_workflow_action` with an action valid for the current state, not submit.
3. Report validation or workflow restrictions as returned; after execution read the document again to confirm the new state.
