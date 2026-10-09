---
name: compound-document-operations
description: Use for large, multi-step ERP document work that may require creating prerequisite records, many child rows, approvals, and follow-up continuation.
tools: schema.search_doctypes, schema.describe_doctype, schema.describe_links, schema.describe_workflow, data.query, data.get_document, data.get_child_records, actions.prepare_document, actions.validate_document, actions.request_create_draft, actions.request_bulk_create, actions.request_update, actions.request_submit, actions.request_workflow_action
---

# Compound Document Operations

Use this skill when the user asks for a task that spans multiple records or documents, such as creating a transaction with many rows, creating missing linked records first, or completing a chain like customer → invoice → payment.

1. Treat the user's request as one compound objective. Do not mark it complete after only creating a prerequisite record.
2. Build a short execution checklist in your private working context: target document, required prerequisites, validation, approval, and next continuation step.
3. Discover target DocTypes, mandatory fields, child tables, and link fields dynamically; never rely on fixed document names or fixed field assumptions.
4. Extract all rows from the current file/context before preparing a large document. If extraction is incomplete, ask one concise clarification instead of guessing.
5. For missing linked records such as supplier, customer, item, account, warehouse, cost center, tax template, or price list, search existing records first.
6. If several prerequisite records are truly missing and required, group them into one `actions.request_bulk_create` approval instead of requesting one approval per record.
7. Use bulk create for any repeated missing master data, not just Items: suppliers, customers, warehouses, accounts, cost centers, price lists, or other dynamically discovered DocTypes when safe.
8. Do not call `actions.prepare_document` repeatedly for every row when the records share the same structure. Prepare/validate representative structure once, then request one bulk approval with all normalized payloads.
9. After a prerequisite approval is created or executed, explicitly keep the original objective active and offer the next continuation action.
10. Do not create duplicate prerequisites. Use exact, normalized, and permission-safe lookups before proposing a new record.
11. For large child tables, prepare one complete target payload rather than one tool call per row.
12. Validate the target document payload before requesting approval whenever validation is available.
13. If validation fails due to missing master data, create or request the missing prerequisites as one bulk approval, then continue the original target after approval.
14. If a single run reaches a safe stop point, return a concise progress summary with: completed prerequisites, pending approvals, remaining target document, and an actionable continuation button/request.
15. For chains of documents, finish each approved document group before moving to the dependent one: master data first, then transaction draft, then submit/payment/workflow only when requested.
16. Keep the user-facing answer compact. Put detailed work in the work log, not in the final answer.
