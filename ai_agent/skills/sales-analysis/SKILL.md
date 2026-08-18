---
name: sales-analysis
description: Use for sales trends, branch comparisons, returns, customers, items, and sales performance investigations.
tools: schema.search_doctypes, schema.describe_doctype, data.query, data.count, data.get_document, reports.search, reports.describe, reports.run, artifacts.create_table, artifacts.create_chart
---

# Sales Analysis

1. Confirm the relevant company, branch and date range from context or data.
2. Use submitted transactions unless the user explicitly asks for drafts.
3. Separate returns from gross sales and calculate net sales.
4. Compare against the previous equivalent period when investigating change.
5. Drill down only when aggregate evidence indicates a meaningful cause.
6. State the data source, filters and period used.
7. Do not claim causation when the evidence only shows correlation.
8. Ask for clarification only when missing context materially changes the result.
