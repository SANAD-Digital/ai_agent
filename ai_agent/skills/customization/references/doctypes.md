# New DocTypes (customize.request_doctype) — Frappe v16

Verified against `frappe/core/doctype/doctype/doctype.py` (`validate`, `validate_name`, `validate_series`,
`validate_fields`, `validate_permissions`, `check_developer_mode`, `on_update`), `frappe/desk/utils.py`
(`validate_route_conflict`) and `frappe/model/delete_doc.py`. Field rules are the same as custom fields
(`fields.md`), except that a DocType's own fields have no `custom_` prefix.

Choose it only when the user needs a **new kind of record** (its own list, form and table): "track site visits",
"a register of contracts". To add data to an existing DocType (Customer, Sales Invoice…) use
`customize.request_field`; to change labels, list columns or title use `customize.request_properties`.

The DocType is always created with `custom = 1`: it lives in the database only. Frappe exports files and makes a
controller only when `custom` is 0, so even with developer mode on, nothing is written into any app.

## 1. Arguments

- `operation`: `create` | `update`.
- `name`: English, starts with a letter; letters, digits, spaces, `-`, `_`; at most 61 characters (the table is
  `tab<name>`). Users can see a translated label later. Not an existing DocType, Page or Workspace (`/app/<slug>`
  must be free), not a core / protected name, not starting with `AI `.
- `module`: optional; default the site's custom module, else `Custom`.
- `fields`: in form order. Each: `label`, `fieldtype` (default Data), `fieldname` (send an English snake_case one
  whenever the label is not English), `options`, `reqd`, `default`, `in_list_view`, `in_standard_filter`,
  `depends_on`, `fetch_from`… Layout: `Section Break` (label = section title), `Column Break`, `Tab Break`.
- Naming `autoname`: `hash` (default, random) | `autoincrement` (1, 2, 3…) | `prompt` (the user types it) |
  `uuid` | `field:<fieldname>` (that field names the record and becomes unique) | `naming_series:` (needs a Select
  field `naming_series` whose options are the series, e.g. `VST-.YYYY.-`) | an expression `VST-.YYYY.-.#####`
  (VST-2026-00001). `format:VST-{YYYY}-{#####}` still works but Frappe 16 warns that it is discouraged.
- Form: `title_field`, `search_fields` (list), `sort_field`, `sort_order`, `quick_entry`, `track_changes`
  (default 1), `allow_rename`, `allow_import`, `is_submittable`, `editable_grid`, `description`.
- `istable: 1` makes a child table: it has no permissions of its own and appears only through a `Table` field of
  another DocType. Create the child table first, then the parent with `{"fieldtype": "Table", "options": "<child>"}`.
- `permissions`: `[{"role": "Sales User", "rights": ["read", "write", "create"]}]`. System Manager (full rights) is
  always added. Later changes go through `customize.request_permissions`.
- `update`: `fields` with an existing `fieldname` change that field; new ones are added (`insert_after` places
  them). Form properties can change too. `istable`, `autoname`, `module` and `permissions` cannot. Standard
  DocTypes are refused (use custom fields).

Worked example — "I want to record site visits: customer, date, status, notes":

```json customize kind=doctype
{"operation": "create", "name": "Site Visit", "autoname": "VST-.YYYY.-.#####",
 "title_field": "customer", "search_fields": ["customer", "status"],
 "fields": [
   {"label": "العميل", "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "reqd": 1, "in_list_view": 1},
   {"label": "تاريخ الزيارة", "fieldname": "visit_date", "fieldtype": "Date", "default": "Today", "in_list_view": 1},
   {"fieldtype": "Column Break"},
   {"label": "الحالة", "fieldname": "status", "fieldtype": "Select", "options": "Planned\nDone\nCancelled", "default": "Planned", "in_standard_filter": 1},
   {"label": "ملاحظات", "fieldname": "notes_section", "fieldtype": "Section Break"},
   {"label": "ملاحظات", "fieldname": "notes", "fieldtype": "Small Text"}],
 "permissions": [{"role": "Sales User", "rights": ["read", "write", "create", "print"]}],
 "purpose": "Track customer site visits"}
```

The card shows the table name, the fields and the form layout (tabs → sections → columns), the naming rule and the
permission rules. Run `customize.check` first.

## 2. Checks you will hit

| code | level | fix |
|---|---|---|
| `dt.name_taken` | critical | a DocType with that name exists: pick another name, or `update` it if it is a custom one |
| `dt.table_exists` | critical | `tab<name>` is left from a deleted DocType: pick another name |
| `dt.name_reserved` / `dt.route_conflict` | critical | core, protected, `AI …`, or `/app/<slug>` taken by a Page / Workspace / DocType |
| `dt.name_settings` | warning | names ending in Settings are treated as configuration and cannot be changed by the assistant later |
| `field.name_reserved` | critical | `name owner creation modified docstatus idx parent…`, `_user_tags…`, or a document method (`save`, `validate`, `meta`…) |
| `field.options` | critical | Link/Table target missing; Table needs a child table (`istable 1`); Select needs one option per line |
| `field.fetch_from` / `field.depends_on` | critical / warning | as in `fields.md` |
| `dt.title_field` / `dt.search_fields` / `dt.sort_field` | critical | must be value fields of this DocType |
| `dt.naming` | critical (field missing, no naming_series field) / warning (`prompt`) / info (`field:` makes it unique) | |
| `dt.child_submittable` | critical | a child table cannot be submittable |
| `perm.role_automatic` / `perm.role_desk_user` | critical | never Guest / All; never write rights to Desk User |
| `perm.risky_right` | warning | delete / submit / cancel for a role other than System Manager |
| `dt.frappe` | critical | Frappe's own `DocType.validate` refused it (message says why, e.g. "Cannot set import as … is not importable": add `allow_import: 1`) |
| `dt.not_custom` / `dt.protected` | critical | update: only custom, non-protected DocTypes |
| `field.fieldtype_change` | critical | update: records exist and Frappe does not allow that type change |
| `field.reqd_existing` / `field.unique_existing` / `dt.submittable_change` | warning | update on a DocType with records |
| `dt.not_exported` | info | developer mode is on; custom = 1 keeps it out of the app files |

## 3. Undo

- Undo of a create deletes the DocType and its table, **only while it has no records** (`dt.has_records`
  critical with the count otherwise), and not while another DocType has a Table field on it (`dt.referenced`).
  There is no "disable" for a DocType.
- Undo of an update restores the previous definition (fields and properties); added fields leave the form, their
  column stays in the table.
