---
name: validate-journal-entry-proposal
description: Validate reviewable Journal Entry proposal groups produced from financial attachment OCR.
allow_implicit_invocation: 1
---

# Validate Journal Entry Proposal

## Purpose
Validate OCR-derived Journal Entry proposals before returning them.

Read-only. Do not create documents.

## Output Structure
The Purchase Invoice AI workspace expects reviewable Journal Entry proposal groups.

Top-level keys must be:
- `invoice_groups`
- `questions`
- `warnings`
- `errors`

Each `invoice_groups[]` item must contain:
- `invoice_group_id`
- `document_meta`
- `files`
- `journal_entry_lines`
- `questions`
- `warnings`
- `errors`

Do not output a top-level `journal_entries` key, Purchase Invoice data, or extra ERP document payloads.

## Balance
For each Journal Entry:
sum(`journal_entry_lines[].debit`) == sum(`journal_entry_lines[].credit`)

If an account is unknown, amounts must still balance; keep account blank and add a warning.
Never invent an account to force validity.

## Account Checks
Every non-empty account must:
- exist
- belong to the company
- have is_group = 0
- use exact ERP account name

Unsupported account => blank it and warn.

## Source Consistency
For each source document:
- `document_meta.document_number` matches supported evidence.
- `document_meta.document_date` matches supported evidence.
- `document_meta.supplier_name` and `document_meta.supplier_tax_id` match supported evidence.
- `custom_bill_no` matches supported document number.
- `custom_bill_date` matches supported date.
- `custom_tax_id` matches supported supplier/payee tax ID.
- `custom_tax_amount` reflects supported tax.
- `user_remark` preserves item/expense meaning.
- `source_file` and `source_page` identify evidence.

Never copy metadata across unrelated documents.

## OCR Arithmetic
Cross-check subtotal + tax + adjustments against grand total.
Correct OCR punctuation only when one interpretation is uniquely supported.
Warn on material correction.

If item detail is incomplete but total is reliable, prefer a summary accounting line over fabricated detail.

## Coverage
Every distinct financial document from the complete selected/read OCR session must either:
- have a Journal Entry proposal, or
- be explicitly named in warnings with the blocking reason.

Never silently drop later pages.
