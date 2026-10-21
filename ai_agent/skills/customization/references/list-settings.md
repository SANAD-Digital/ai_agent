# List view settings (customize.request_list_settings) — Frappe v16

Verified against `frappe/desk/doctype/list_view_settings/list_view_settings.py`, `public/js/frappe/list/list_view.js`,
`list_settings.js`, `list_sidebar_group_by.js` and `core/doctype/doctype/doctype.py` (`check_sort_field`)
(Frappe 16.28). Every example passes `customize.check` (kind `list_settings`).

Arguments: `operation` (`update` | `reset`), `doctype`, `settings`, `columns`, `sort_field`, `sort_order`, `purpose`.

## 1. When to use it

| Need | Use |
|---|---|
| Which columns the list shows, and their order | **this tool**, `columns` |
| Hide the total count / comment counts / sidebar stats, stop live refresh or infinite scroll, show tags, allow editing cells | this tool, `settings` |
| The default sort of the list | this tool, `sort_field` + `sort_order` |
| A filter field in the list's standard filter bar | `customize.request_properties` (`in_standard_filter`) |
| Group-by fields in the list sidebar | not a site setting: each user picks them in the sidebar ("Edit"); Frappe stores them per user |

It changes the list **for every user**. A user's own sort or column width choice still wins on their screen.

## 2. Where Frappe keeps it

- `List View Settings` (one record named after the DocType): the flags and `fields`, the column order.
- Columns appear only for fields with **In List View**, so — like Frappe's own List Settings dialog — the tool sets
  In List View = 1 on the listed fields and 0 on the fields you take out.
- The default sort is the DocType's `sort_field` / `sort_order` (Property Setters, as Customize Form writes them).
- The title field (or ID) is always the first column and the status indicator the next: you do not list them.

## 3. Arguments

| Key | Format |
|---|---|
| `columns` | fieldnames in display order, after the title column: `["customer", "grand_total", "due_date"]`. Not Table/Section/HTML fields; not virtual fields |
| `settings` | `disable_count`, `disable_comment_count`, `disable_sidebar_stats`, `disable_auto_refresh`, `disable_automatic_recency_filters` (the automatic "last 90 days" filter on big tables), `disable_scrolling`, `show_tags`, `allow_edit` — each 0 or 1 |
| `sort_field` | a fieldname, `creation` or `modified` (comma-separated for several) |
| `sort_order` | `ASC` or `DESC` |

```json customize kind=list_settings
{"operation": "update", "doctype": "ToDo",
 "columns": ["status", "priority", "date", "allocated_to"],
 "settings": {"disable_comment_count": 1, "show_tags": 1},
 "sort_field": "date", "sort_order": "ASC",
 "purpose": "Tasks list: due date first, with owner and priority"}
```

`reset` deletes the List View Settings record so Frappe's defaults come back (In List View and sort stay as they are):
`{"operation": "reset", "doctype": "ToDo"}`. Undo restores the exact earlier state (record, In List View, sort).

## 4. Checks and how to fix them

| Code | Level | Fix |
|---|---|---|
| `list_settings.doctype` | critical | not a core, Single, child-table or protected DocType |
| `list_settings.read` | critical | the requester must read the DocType |
| `list_settings.column` | critical | the field exists and holds a value (not Table/Section/HTML/Button, not virtual) |
| `list_settings.column` | warning | a permission-level field shows empty to users without the level; a field hidden on the form still shows in the list |
| `list_settings.column` | info | the title/ID and status columns are always there |
| `list_settings.columns_many` | warning | more than 8 columns scroll sideways on a laptop |
| `list_settings.sort` | critical | `sort_field` must be a field of the DocType (or creation/modified…) |
| `list_settings.reset` | critical | nothing to reset: the DocType already uses Frappe's defaults |
| `list_settings.frappe` | critical | Frappe's own validation failed in the dry run |
