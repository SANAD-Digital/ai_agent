---
name: financial-attachments-to-journal-entry
description: Convert complete Financial Attachment Session OCR into reviewable ERPNext Journal Entry proposal groups.
allow_implicit_invocation: 1
tools: financial_attachments.get_session_ocr_text, data.query, memory.search
tool_call_limits: financial_attachments.get_session_ocr_text=1, data.query=12, memory.search=4
tool_result_limits: data.query=10, memory.search=5
response_format: JSON Object
required_output_keys: invoice_groups, questions, warnings, errors
finalization_max_tokens: 32768
finalization_timeout: 300
final_output_instruction: Return one complete compact JSON object using the output contract defined by this skill, without Markdown, indentation, progress text, or repeated evidence.
---

# Financial Attachments To Journal Entry

## Purpose
Analyze OCR evidence from a `Financial Attachment Session` and return Journal Entry proposals only.

The output target is always `Journal Entry`.

When the prompt includes a `Financial Attachment Session` name, the task is already defined: read that session and produce the required Journal Entry proposal JSON. Do not ask the user what the task is.

Invoices, receipts, vouchers, POS slips, tax invoices, and other attachments are source evidence only. Never create, prepare, describe, or propose:
- Purchase Invoice
- Purchase Invoice Item
- Item
- Supplier
- Cash Supplier

Do not create any ERP document during analysis.

## Required Supporting Tools And Skills
Use these read-only tools directly when needed:
- `financial_attachments.get_session_ocr_text` to read the complete ready OCR text for the target session.
- `data.query` to retrieve only narrowly matched `Account` candidates for the target company.
- `memory.search` to look for approved mappings for supplier/payee/item/account hints.

Apply the implicit supporting skills:
- `accounting-memory-to-account`
- `validate-journal-entry-proposal`

Never load the whole chart of accounts. Query `Account` only when a line needs an account, using:
- fields: `name`, `account_name`, `account_type`, `root_type`
- mandatory filters: `company = target company`, `is_group = 0`, `disabled = 0`
- a narrow `name like` or `account_name like` filter based on the expense/payment nature
- limit 5 initially, and at most 10 after one broader retry

Use the exact returned `Account.name`. If no reliable candidate is found, leave the account blank and add a question or warning.

## Source Of Truth
For OCR workflows, the source of truth is the existing `Financial Attachment Session` and its `Financial Attachment Session Page` rows.

Do not rely only on an attachment preview, original-PDF summary, truncated payload, or the first 3 pages.

If the current payload is partial, retrieve the complete ready OCR page set from ERP using read-only data tools.

## Complete-Session Rule
Before accounting analysis:
1. Identify the target `Financial Attachment Session`.
2. Read: name, company, source_run, page_count, selected_page_count, read_page_count, status.
3. Read every selected page whose status is `Read`.
4. Order by page_number ascending.
5. Paginate until all required pages are obtained.
6. Never treat a tool page-size/result limit as the end of the OCR session.
7. Verify retrieved ready-page count against session counters.
8. If OCR is still incomplete, warn instead of silently analyzing a partial set.

For a completed session with 23 selected/read pages, consider all 23 pages.

## OCR Evidence
Use primarily:
- file_name
- page_number
- text_preview
- warnings_json
- compact OCR confidence when useful

Do not load all bbox coordinates unless layout evidence is specifically needed.

Treat OCR as noisy evidence. Never invent missing values.
You may normalize digits, whitespace, common OCR punctuation, and date formatting.
Repair an OCR decimal/digit only when financial arithmetic uniquely supports the correction, and add a warning if material.

## Document Detection And Grouping
Identify every distinct source document across all pages.

Strong boundaries:
- document/invoice/voucher number
- document date
- supplier/payee
- supplier tax ID
- grand total
- explicit continuation markers

Rules:
- different document numbers => different source documents;
- same supplier does not imply same document;
- only merge pages when evidence supports continuation;
- avoid duplicate scans/double counting.

Unless the user explicitly requests combination, prepare one Journal Entry proposal per distinct source document.

### Combined Journal Entry Mode
When the request explicitly asks for one combined Journal Entry for the complete session:
- return exactly one object inside `invoice_groups`;
- place the accounting rows for every distinct source document in that single object's `journal_entry_lines`;
- never create a separate group or Journal Entry proposal per page or per document;
- include every considered source file/page under the combined group's `files`;
- preserve each document independently on its relevant rows through `custom_bill_no`, `custom_bill_date`, `custom_cash_supplier`, `custom_tax_id`, `custom_tax_amount`, `source_file`, `source_page`, and `line_meta`;
- keep each document's debit and balancing credit evidence as separate rows when needed for traceability; do not net unrelated documents into an untraceable summary;
- validate that the complete combined `journal_entry_lines` set is numerically balanced.

## Financial Extraction
For each document extract when supported:
- source file/page
- document number/date
- supplier/payee and tax ID
- item/expense descriptions
- quantity/rate/line amount
- subtotal/tax/grand total
- paid/outstanding amount
- payment method

Preserve item/expense meaning in debit-row `user_remark`.

If item rows are unreliable but totals are reliable, use one summary debit row and warn.

## Journal Entry Construction
1. Build debit expense/cost line(s).
2. Use `accounting-memory-to-account` first.
3. Build balancing credit line from supported ERP configuration/default/memory.
4. If balancing account is unknown, leave `account` blank but keep the correct credit amount and warn.
5. Never invent cash/bank/payable/clearing accounts.
6. Never create Supplier/Cash Supplier.
7. Use exact ERP Account names.
8. Leaf accounts only (`is_group = 0`).
9. Respect company.

`custom_tax_amount` stores evidenced tax. Do not automatically create a separate tax line unless explicit accounting configuration or user instruction requires it.

## Account Selection Order
1. approved exact supplier-item memory
2. approved normalized item match
3. approved supplier/category mapping
4. explicit ERP default/configuration
5. reasoned valid leaf account when evidence is strong
6. otherwise blank account + warning

Never choose randomly.

## Creation Safety
Proposal only. Do not create Journal Entry until explicitly requested after review.
Do not call Purchase Invoice creation paths.

## Required Workspace Output Contract
Return valid JSON only, with no Markdown. The workspace consumes `invoice_groups`; do not return a top-level `journal_entries` key.

```json
{
  "invoice_groups": [
    {
      "client_readable_summary": "",
      "invoice_group_id": "GROUP-001",
      "document_meta": {
        "document_type": "tax_invoice|simplified_tax_invoice|pos_receipt|payment_voucher|receipt_voucher|bill|financial_document|unknown",
        "document_title": "",
        "supplier_name": "",
        "supplier_tax_id": "",
        "supplier_phone": "",
        "supplier_address": "",
        "document_number": "",
        "document_date": "",
        "currency": "SAR",
        "subtotal": 0,
        "discount_amount": 0,
        "taxable_amount": 0,
        "tax_amount": 0,
        "grand_total": 0,
        "payment_method": "",
        "source_file": "",
        "source_pages": [1],
        "evidence": {
          "document_number_text": "",
          "document_date_text": "",
          "supplier_text": "",
          "tax_id_text": "",
          "total_text": ""
        }
      },
      "files": [
        {
          "file_id": "",
          "file_name": "",
          "pages": [
            {
              "page": 1,
              "text_preview": ""
            }
          ]
        }
      ],
      "journal_entry_lines": [
        {
          "line_no": 1,
          "account": "",
          "account_match_method": "exact|memory|similarity|needs_user_choice",
          "account_confidence": 0,
          "account_suggestions": [],
          "party_type": "",
          "party": "",
          "supplier_action": "matched|create|needs_user_choice|not_applicable",
          "debit": 0,
          "credit": 0,
          "description": "",
          "custom_cash_supplier": "",
          "custom_bill_no": "",
          "custom_bill_date": "",
          "custom_tax_id": "",
          "custom_tax_amount": 0,
          "user_remark": "",
          "cost_center": "",
          "source_file": "",
          "source_page": 1,
          "line_meta": {
            "document_number": "",
            "document_date": "",
            "supplier_name": "",
            "supplier_tax_id": "",
            "line_amount": 0,
            "tax_amount": 0,
            "source_text": ""
          }
        }
      ],
      "questions": [],
      "warnings": [],
      "errors": []
    }
  ],
  "questions": [],
  "warnings": [],
  "errors": []
}
```

Every `invoice_groups[]` object represents one distinct source document unless the user explicitly asks to combine documents. In Combined Journal Entry Mode, `invoice_groups` must contain exactly one object representing the complete session, while document identity remains on every relevant row.

Every group must include a balanced `journal_entry_lines` set:
- debit rows for evidenced expenses/costs/tax handling;
- one balancing credit row when the credit amount is known;
- if the credit account is unknown, keep `account` blank on the credit row and add a warning, but keep debit and credit totals balanced.

Copy document metadata into each relevant row:
- `custom_bill_no` = evidenced document number;
- `custom_bill_date` = evidenced document date;
- `custom_cash_supplier` = evidenced supplier/payee name when no ERP party is selected;
- `custom_tax_id` = evidenced supplier/payee tax ID;
- `custom_tax_amount` = evidenced tax amount for the document or line.

## Final Validation
Before returning:
- every selected/read OCR page was considered;
- every distinct financial document is represented or specifically warned about;
- when combination was requested, exactly one `invoice_groups` object contains all represented documents;
- no Purchase Invoice output exists;
- no top-level `journal_entries` output exists;
- every Journal Entry is numerically balanced;
- amounts are numbers;
- bill/date/tax/source traceability is preserved;
- exact valid leaf account names are used;
- no unsupported account is invented.

## Mandatory Workflow Completion

This is a compound accounting workflow.

Reading the OCR session is only a prerequisite and MUST NEVER be
treated as completion of the user's request.

After OCR text has been retrieved, continue automatically in the same
agent run through all required stages:

1. Read complete OCR session
2. Detect distinct source documents
3. Extract financial evidence
4. Resolve accounting accounts
5. Build Journal Entry proposals under `invoice_groups[].journal_entry_lines`
6. Validate every Journal Entry group
7. Return the required workspace JSON

Do not stop after:
- get_session_ocr_text
- loading read-financial-attachment-session
- confirming page count
- confirming OCR completion
- detecting documents
- resolving accounts

These are intermediate steps only.

A successful run is complete ONLY when the final Journal Entry JSON
has been produced according to the Required Workspace Output Contract.

## No Intermediate Final Response

Never use the final answer to report workflow progress.

Forbidden final responses include:
- "OCR text received"
- "23 pages received"
- "all pages analyzed"
- "I will now prepare the entries"
- any other progress/status message

If the OCR data is available, continue working.

The final answer MUST contain the workspace JSON with `invoice_groups`.
