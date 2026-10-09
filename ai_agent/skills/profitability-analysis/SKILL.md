---
name: profitability-analysis
description: Use for profit margin analysis, cost analysis, revenue vs cost comparisons, and profitability insights.
tools: schema.search_doctypes, schema.describe_doctype, data.query, data.count, reports.search, reports.describe, reports.run, artifacts.create_table, artifacts.create_chart
---

# Profitability Analysis

1. Only analyze cost fields if the user has appropriate permlevel/role access.
2. Clarify the cost basis: gross profit vs net profit.
3. Use GL Entry for financial data, Sales Invoice for revenue.
4. Respect company and currency context.
5. Compare periods for trend analysis.
6. State assumptions about cost allocation and currency used.
7. Distinguish between correlation and causation in profit changes.
