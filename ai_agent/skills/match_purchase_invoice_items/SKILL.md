---
name: match_purchase_invoice_items
description: Match extracted purchase invoice item rows to ERPNext Items using barcodes, supplier codes, item supplier rows, names, descriptions, and purchase history.
---

# Match Purchase Invoice Items

This skill is only for creating or matching ERPNext Purchase Invoice item rows. Do not load or use it for Financial Attachment Session OCR, Journal Entry suggestions, receipts, vouchers, or the OCR-to-Agent Workspace flow.

1. Match by barcode, supplier item code, Item Supplier, exact item code, item name, description, and previous purchases from the same supplier.
2. Respect user permissions for Item and related records.
3. Return best match, alternatives, confidence, and match method for every row.
4. Mark unresolved rows as `Unmatched`; do not create Item records automatically.
5. Keep the original extracted line text for manual review.
