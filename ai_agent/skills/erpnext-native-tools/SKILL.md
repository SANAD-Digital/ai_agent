---
name: erpnext-native-tools
description: ERPNext's own helpers for item/party details, fiscal year, exchange rates, stock balance, invoice payments, and standard document mapping.
tools: erp.item_details, erp.party_details, erp.fiscal_year, erp.exchange_rate, erp.stock_balance, erp.invoice_payments, actions.request_mapped_draft
---

# ERPNext native tools

1. Prefer these over manual queries: they apply ERPNext's own pricing, UOM, account and tax rules.
2. To create an invoice or delivery note from a Sales Order, a Purchase Order from a Material Request, or a Purchase Receipt from a Purchase Invoice, use actions.request_mapped_draft: it builds the draft with ERPNext's standard mapping and asks for approval. Do not rebuild these documents field by field.
3. The draft is only created after approval, and stays a draft (the agent never submits).
