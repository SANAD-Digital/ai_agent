---
name: accounting
title: Accounting
description: Load for any accounting or profitability question: revenue, expenses, net profit, gross profit and margin by item/customer/branch, cash and bank balances, account balances and movements, general ledger, trial balance, profit and loss, balance sheet, cash flow, journal entries, party ledger/statement, and diagnosis of why a GL entry is missing or why an invoice does not show in a customer or supplier statement.
tools: metrics.query, data.query, reports.run, reports.describe, erp.fiscal_year, erp.invoice_payments, diagnose.document, setup.check, knowledge.search, artifacts.create_table, artifacts.create_chart
---

# Accounting and profitability

## Fast paths (no schema discovery needed)

| Question | Call |
|---|---|
| Revenue / expenses for a period | `metrics.query {metric:"revenue", period:"this_month"}` / `"expenses"`; dims: account, cost_center |
| Net profit | `metrics.query {metric:"net_profit", period:"this_fiscal_year", compare:"same_period_last_year"}` (composite: revenue − expenses from GL) |
| Biggest expenses | `metrics.query {metric:"expenses", dimensions:["account"], top:10}` |
| Profit by branch / cost center | revenue and expenses with `dimensions:["cost_center"]` |
| Cash / bank balance | `cash_balance` / `bank_balance` (balance at period end; dim: account) |
| Company position (one call) | `metrics.query {metric:"company_position"}` (revenue, expenses, cash, banks, receivables, payables, stock value) |
| Gross profit / margin by item, customer, group | `reports.run "Gross Profit"` (below) |
| Fiscal year dates | `erp.fiscal_year {date, company}` |

Receivables/payables/collections/payments metrics belong to the sales and purchasing skills; they work here too (`receivables`, `payables`, `collections`, `payments_made`).

## Reports (pass every required filter; no defaults are filled)

- **Gross Profit** `{company*, from_date*, to_date*, group_by: "Invoice"|"Item Code"|"Item Group"|"Brand"|"Warehouse"|"Customer"|"Customer Group"|"Territory"|"Sales Person"|"Project"|"Cost Center"|"Monthly"|"Payment Term", include_returned_invoices:1, item_group, warehouse, cost_center:[..]}`: selling amount, buying amount (valuation), gross profit, %.
- **General Ledger** `{company*, from_date*, to_date*, account:[..], party_type, party:[..], voucher_no, cost_center:[..], categorize_by: "Categorize by Voucher (Consolidated)"|"Categorize by Account"|"Categorize by Party"|"Categorize by Voucher"}`: opening, rows, closing. Use it for account and party statements.
- **Trial Balance** `{company*, fiscal_year*, from_date, to_date, cost_center:[..], show_zero_values:0}`.
- **Trial Balance for Party** `{company*, fiscal_year*, party_type*: "Customer"|"Supplier", party, from_date, to_date}`.
- **Profit and Loss Statement** `{company*, filter_based_on:"Date Range", period_start_date, period_end_date, periodicity*: "Monthly"|"Quarterly"|"Half-Yearly"|"Yearly", selected_view:"Report"}`; with `filter_based_on:"Fiscal Year"` pass `from_fiscal_year`, `to_fiscal_year` instead of dates. `selected_view` "Growth" / "Margin" give % views.
- **Balance Sheet** and **Cash Flow**: same filters as P&L (no selected_view).
- **Customer Ledger Summary** / **Supplier Ledger Summary** `{company, from_date*, to_date*, party}`; **COGS By Item Group** `{company, from_date, to_date}`; **Stock and Account Value Comparison** `{company*, as_on_date}`.

## `data.query` shapes

Cost guard: child tables and any DocType over 10k rows are rejected without a date filter (or `parent.<date>`), a `parent` filter, or two `=`/`in` filters on Link/Select fields. Always send a date range.

Account balance up to a date (include the account's children with `descendants of`):
```
data.query {doctype:"GL Entry", fields:[{field:"debit",aggregate:"SUM",alias:"dr"},{field:"credit",aggregate:"SUM",alias:"cr"}],
  filters:[{field:"account",operator:"descendants of",value:"Cash - X"},{field:"is_cancelled",operator:"=",value:0},{field:"posting_date",operator:"<=",value:"2026-09-30"},{field:"company",operator:"=",value:"X"}]}
```
Balance = dr − cr (assets/expenses) or cr − dr (liabilities/income/equity).

Monthly movement of an account: same filters with a date range, `group_by:[{field:"posting_date",bucket:"month",alias:"month"}]`. By cost center: `group_by:["cost_center"]`. Entries of one voucher: filters `voucher_type`, `voucher_no`.

Chart of accounts lookup: Account, fields `name, account_name, account_number, root_type, account_type, parent_account, is_group, account_currency, freeze_account`; filter `company`, `account_name like_normalized`. root_type: Asset, Liability, Income, Expense, Equity. account_type examples: Bank, Cash, Receivable, Payable, Stock, Cost of Goods Sold, Tax, Income Account, Expense Account.

Journal entries: Journal Entry `voucher_type, posting_date, title, total_debit, remark, cheque_no, is_opening`, `docstatus=1` (on ERPNext v16 the header note is `remark`; `title` and `user_remark` are hidden legacy fields: read them, never write them); lines: Journal Entry Account (`parent_doctype:"Journal Entry"`) `account, party_type, party, debit, credit, reference_type, reference_name, cost_center`. JE voucher_type values: Journal Entry, Bank Entry, Cash Entry, Credit Note, Debit Note, Contra Entry, Write Off Entry, Opening Entry, Depreciation Entry, Exchange Rate Revaluation, …

## Key fields

- **GL Entry**: posting_date, account, party_type, party, voucher_type, voucher_no, against_voucher_type, against_voucher, debit, credit (company currency), debit_in_account_currency, credit_in_account_currency, account_currency, cost_center, project, is_cancelled, is_opening, company, fiscal_year, remarks.
- **Payment Ledger Entry** (what statements and aging read): party_type, party, account, voucher_no, against_voucher_no, amount, due_date, delinked.
- **Company**: default_currency, default_receivable_account, default_payable_account, default_income_account, default_expense_account, round_off_account, exchange_gain_loss_account, default_cash_account, cost_center, accounts_frozen_till_date.

## Rules

1. Read only. Never propose journal entries as done; any entry goes through the documents skill with approval.
2. GL: always `is_cancelled = 0`. Amounts in `debit`/`credit` are company currency; never add across companies with different currencies.
3. State company, period, currency and basis (GL vs invoices) in one line. Revenue from GL ≠ net sales from invoices (journals, rounding, other income); say which one.
4. Gross profit uses valuation (cost) fields; they may be hidden by permission level. If hidden, say so instead of estimating.
5. Net profit here = revenue − expenses from GL (cost of sales included). Do not allocate costs the books do not allocate.
6. Output: `artifacts.create_chart` for trends and comparisons, `artifacts.create_table` for lists over ~10 rows. A report that rejects its filters: `reports.describe` once, then retry.

## Diagnose: accounting entry missing for a document

1. `diagnose.document {doctype, name}`.
2. Causes in order: not submitted → no gl_entry → missing company default accounts (`setup.check {area:"accounts", company}` lists them) → posting date on/before `accounts_frozen_till_date` → closed Accounting Period for that document type → Period Closing Voucher covering the date.
3. Explain cause and steps (cite `knowledge.search` when the guide has them). Settings are changed by an administrator in the desk, never through the agent.

## Diagnose: invoice missing from a party statement

1. `diagnose.document` on the invoice.
2. Check in order: docstatus; the party on the invoice; the company (statement must use the same company); party account (`debit_to` / `credit_to`) vs the statement's account filter; currency (statement shows account currency); payment_ledger row present.
3. Shown as settled: `erp.invoice_payments {doctype, name}` lists the allocated payments.
4. Tell the user which statement filter to change, or what is wrong with the invoice.
