---
name: match_purchase_invoice_supplier
description: Match extracted purchase invoice supplier details to ERPNext Supplier records using permission-safe lookups.
---

# Match Purchase Invoice Supplier

1. Search existing Supplier records before suggesting a new one.
2. Match by tax id, commercial registration, exact name, normalized trade name, phone, email, and prior purchases.
3. Respect user permissions and company context.
4. Return `matched_supplier`, `match_method`, `confidence`, and alternatives.
5. Do not create Supplier records automatically.
