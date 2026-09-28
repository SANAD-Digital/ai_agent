---
name: frappe-schema-discovery
description: Discover DocTypes, fields, relationships, and reports before querying unknown data structures.
tools: schema.search_doctypes, schema.describe_doctype, schema.describe_links, schema.describe_workflow, skills.read_reference
---

# Frappe Schema Discovery

0. For common DocTypes (Sales Invoice, Sales Invoice Item, Purchase Invoice, Purchase Invoice Item, Payment Entry, Journal Entry, Journal Entry Account, GL Entry, Stock Ledger Entry, Bin, Item, Customer, Supplier, Sales Order, Delivery Note) read the prepared summary first: skills.read_reference(skill_name="frappe-schema-discovery", reference="sales-invoice"), i.e. the DocType name in lower case with dashes. Use schema.describe_doctype with fields_filter only for fields the summary does not list (e.g. site custom fields).
1. Search for other DocTypes before querying unknown data.
2. Use schema.search_doctypes to find relevant DocTypes by name or description.
3. Use schema.describe_doctype to get field definitions and relationships.
4. Use schema.describe_links to understand link relationships between DocTypes.
5. Use schema.describe_workflow to understand approval workflows.
6. Use reports.search and reports.describe to discover available reports.
7. Never assume fieldnames - always verify from schema metadata.
8. Respect user permissions when describing DocTypes.
9. Return concise metadata, not full field dumps.
