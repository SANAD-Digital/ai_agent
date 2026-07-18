---
name: report-analysis
description: Use for discovering, running, and analyzing Frappe/ERPNext standard reports.
---

# Report Analysis

1. First discover available reports using reports.search.
2. Use reports.describe to understand filters and columns before running.
3. Do not assume filters - read report metadata first.
4. Run reports with appropriate filters based on user context.
5. Respect report-level permissions and role restrictions.
6. Present report results in a clear, summarized format.
7. Do not run Administrator-only reports for regular users.
8. For printable reports, return only the sections the user requested; do not force title, source, notes, totals, or commentary.
9. Do not include prefaces, progress narration, notes, or calculation commentary in the final report unless the user asks for them.
10. Never put Markdown headings or tables inside raw HTML tags; use plain Markdown so the UI can render and print it.
11. If the user asks for merged companies, aggregate across all requested companies and avoid per-company sections.
12. If the user says not monthly, do not add month columns or month notes.
13. If the user asks for filters plus table only, return filter lines and the table only; remove report title, source, and all text after the table.
