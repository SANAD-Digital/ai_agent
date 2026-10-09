---
name: data-quality-audit
description: Use for detecting missing data, duplicates, inconsistencies, and data quality issues.
tools: schema.search_doctypes, schema.describe_doctype, schema.describe_links, data.query, data.count, data.get_document, data.get_child_records, artifacts.create_table
---

# Data Quality Audit

1. Read-only analysis - any fixes require separate action approval.
2. Check for missing mandatory fields.
3. Detect duplicate records by name, naming series, or key fields.
4. Identify orphaned records (linked records that no longer exist).
5. Flag inconsistent data (e.g., negative quantities, future dates).
6. State the DocType, filters, and criteria used.
7. Provide actionable recommendations, not just findings.
