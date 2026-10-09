---
name: diagnose-party-statement
description: Why an invoice does not appear in a customer or supplier statement/ledger.
tools: diagnose.document, data.get_document, erp.invoice_payments, knowledge.search
---

# Diagnose: invoice missing from a party statement

1. Call diagnose.document on the invoice.
2. Check, in order: docstatus; the party on the invoice; the company (the statement must be filtered by the same company); the party account (debit_to / credit_to) used by the statement filter; currency differences (the statement shows the account currency); payment_ledger presence.
3. If the invoice is there but shows as settled, call erp.invoice_payments to list the payments allocated against it.
4. Explain which statement filter to change, or what is wrong with the invoice.
