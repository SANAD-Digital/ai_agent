---
name: inventory-analysis
description: Use for stock levels, warehouse analysis, item movement, slow-moving items, and inventory optimization.
tools: schema.search_doctypes, schema.describe_doctype, data.query, data.count, data.get_document, reports.search, reports.describe, reports.run, artifacts.create_table, artifacts.create_chart
---

# Inventory Analysis

1. Never mix current stock levels with historical movement data.
2. Use aggregation for large datasets instead of fetching full ledgers.
3. Respect warehouse and company permissions.
4. Distinguish between qty-based and value-based analysis.
5. Use Stock Ledger Entry for movement history.
6. Use Bin for current stock levels.
7. Check for negative stock and flag anomalies.
8. State warehouse, company, and period filters used.
