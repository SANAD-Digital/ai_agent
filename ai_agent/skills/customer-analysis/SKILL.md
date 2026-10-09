---
name: customer-analysis
description: Use for customer activity, purchasing patterns, lifetime value, and customer segmentation analysis.
tools: schema.search_doctypes, schema.describe_doctype, data.query, data.count, data.get_document, reports.search, reports.describe, reports.run, artifacts.create_table, artifacts.create_chart
---

# Customer Analysis

1. Confirm company and date range context.
2. Use Sales Invoice and Sales Order for customer activity.
3. Calculate customer lifetime value from submitted transactions.
4. Identify top customers by revenue and volume.
5. Detect inactive or dormant customers.
6. Respect customer and company permissions.
7. Do not expose PII unless user has appropriate permissions.
