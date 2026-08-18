---
name: payables-analysis
description: Use for accounts payable aging, supplier outstanding balances, and payment analysis.
tools: schema.search_doctypes, schema.describe_doctype, data.query, data.count, data.get_document, reports.search, reports.describe, reports.run, artifacts.create_table, artifacts.create_chart
---

# Payables Analysis

1. Separate outstanding amounts from grand totals.
2. Use Payment Ledger Entry and Purchase Invoice for payable data.
3. Analyze aging buckets: current, 30, 60, 90, 120+ days.
4. Respect party and company permissions.
5. Filter by company and currency.
6. State the reporting date and filters used.
