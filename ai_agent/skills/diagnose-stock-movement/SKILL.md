---
name: diagnose-stock-movement
description: Why a stock movement does not show for an invoice, delivery note, receipt or stock entry.
tools: diagnose.document, data.get_document, data.get_child_records, knowledge.search
---

# Diagnose: stock movement not showing

1. Call diagnose.document on the document (use the open document when the user says "this invoice").
2. Read the checks in this order and stop at the first failing cause:
   - docstatus: only submitted documents move stock.
   - update_stock (invoices): off with no linked Delivery Note / Purchase Receipt means the invoice never moves stock; if a linked document exists, the movement is there.
   - is_stock_item: non-stock items never appear in the stock ledger.
   - warehouse: stock items need a warehouse on the row.
   - stock_ledger_entry: none on a submitted document points to a posting problem; suggest checking the Error Log or reposting.
3. Explain the cause and the fix as steps in the user's language. Never change the document yourself; if a field change on a draft is the fix and the user asks for it, use the normal approval request.
4. A check marked unknown means the user lacks permission to see it: say so.
