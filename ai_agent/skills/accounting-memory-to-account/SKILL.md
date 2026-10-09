---
name: accounting-memory-to-account
description: Resolve the best leaf Journal Entry account from approved accounting memory and current ERP accounts.
allow_implicit_invocation: 1
tools: data.query, memory.search
---

# Accounting Memory To Account

## Purpose
Resolve the best Journal Entry account for an OCR item/expense by reusing approved historical accounting memory before model inference.

Read-only skill.

## Principle
Do not send all items/accounts/memory to the model.
Retrieve only memory relevant to current company + supplier/payee + tax ID + item/expense description + supplier code/barcode when available.

## Memory Sources
Discover dynamically.

Preferred when it exists:
- dedicated mapping DocType such as `AI Accounting Mapping`

Fallback:
- structured `AI Memory` for the current agent/company

Do not assume a dedicated mapping DocType exists.

## Retrieval Priority
1. company + supplier tax ID + supplier item code
2. company + supplier + supplier item code
3. company + supplier tax ID + normalized item description
4. company + supplier + normalized item description
5. approved alias/normalized description
6. strong historical text similarity

Prefer approved mappings with stronger approval/use history, same supplier/company, and recent successful use.

Return only the best 1-5 relevant candidates.

## Validation
Before choosing:
- Account exists
- Account belongs to target company
- is_group = 0
- use exact `Account.name`

Use a narrow `data.query` on `Account` when current leaf accounts are needed. Always filter by the target company, `is_group = 0`, and `disabled = 0`; request only `name`, `account_name`, `account_type`, and `root_type`, with limit 5 initially and at most 10 after one broader retry. Use `memory.search` when approved historical mappings may exist.

If exact/high-confidence approved memory exists, prefer it over inference.
If reliable candidates conflict, do not guess.
If no reliable memory exists, calling skill may reason from valid leaf accounts; otherwise leave blank and warn.

## Supplier Handling
Supplier/payee is evidence and a match key only.
Never create Supplier or Cash Supplier.

## Learning Boundary
Model suggestions are not durable memory.
Only business-approved/persisted choices should later become memory.
Do not write memory during proposal generation.
