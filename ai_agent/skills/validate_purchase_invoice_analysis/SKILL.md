---
name: validate_purchase_invoice_analysis
description: Validate extracted purchase invoice data, deterministic totals, duplicate risk, and required ERPNext references before draft creation.
tools: schema.describe_doctype, data.query, data.get_document
---

# Validate Purchase Invoice Analysis

1. Recalculate subtotal, discounts, tax, and grand total deterministically from item rows.
2. Check company, supplier, bill number, dates, currency, item rows, quantities, rates, UOMs, warehouse, expense account, and cost center.
3. Check duplicate risk by supplier, bill number, bill date, currency, and grand total.
4. Return blockers in `errors` and review items in `warnings`.
5. Do not create or update ERPNext documents.
