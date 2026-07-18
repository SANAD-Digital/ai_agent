---
name: frappe-schema-discovery
description: Discover DocTypes, fields, relationships, and reports before querying unknown data structures.
---

# Frappe Schema Discovery

1. Always search for DocTypes before querying unknown data.
2. Use schema.search_doctypes to find relevant DocTypes by name or description.
3. Use schema.describe_doctype to get field definitions and relationships.
4. Use schema.describe_links to understand link relationships between DocTypes.
5. Use schema.describe_workflow to understand approval workflows.
6. Use reports.search and reports.describe to discover available reports.
7. Never assume fieldnames - always verify from schema metadata.
8. Respect user permissions when describing DocTypes.
9. Return concise metadata, not full field dumps.
