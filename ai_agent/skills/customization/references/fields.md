# Custom fields (customize.request_field) — Frappe v16

Arguments: `doctype`, `operation` (`add` | `update` | `remove`), `fields` (1–10), `reason`. Unknown keys are refused,
not ignored. Verified against `frappe/custom/doctype/custom_field/custom_field.py` and `frappe/model/__init__.py`.

## Which DocTypes

- Standard DocTypes only. Not core DocTypes (`frappe.model.core_doctypes_list`: DocType, DocField, Custom Field, User,
  Role, Server Script…), not Single DocTypes (settings), not custom DocTypes (`custom = 1`, edited as a DocType).
- `customize.inspect` says `customizable: false` with the reason.

## Fieldname

- ASCII `snake_case`, pattern `^[a-z][a-z0-9_]{1,60}$`. The tool adds `custom_` when missing.
- Why ASCII: when Frappe derives a name from the label it keeps any letter (`c.isalpha()`), so an Arabic label becomes
  `custom_اسم_العميل`. The tool refuses that; give an English fieldname and an Arabic `label`.
- Reserved, never use: `name`, `owner`, `creation`, `modified`, `modified_by`, `docstatus`, `idx`, `doctype`,
  `parent`, `parentfield`, `parenttype`, `flags`, `file_list`, and anything that is already a field or a document
  attribute (`check_fieldname_conflicts`).
- The record is named `<DocType>-<fieldname>` (for `update` / `remove` give the fieldname).

## Field types and `options`

| fieldtype | options | Notes |
|---|---|---|
| Data | empty, or `Email` / `Phone` / `URL` / `Name` (validation) | varchar(140); `length` up to 255+ |
| Small Text, Text, Long Text, Text Editor | — | text columns |
| Int, Float, Percent | — | `precision` for Float/Percent ("" = system default, "0"–"9") |
| Currency | currency fieldname on the same form (e.g. `currency`), or empty (company currency) | |
| Check | — | `default` "0" or "1" only |
| Date, Datetime, Time, Duration | — | `default: "Today"` works for Date |
| Select | one option per line (`"\nOpen\nClosed"`: a leading empty line allows blank) | |
| Link | the target DocType name, exactly | target must exist |
| Dynamic Link | the fieldname of a **Link to DocType** field on the same form | |
| Table | a child DocType (`istable = 1`) | rows live in the child table |
| Table MultiSelect | a child DocType that has a Link field | |
| Attach, Attach Image | — | `image_field` needs Attach Image |
| Phone, Rating, Color, Read Only, HTML | — | Read Only/HTML show values or markup, store nothing useful |
| Section Break, Column Break, Tab Break | — | layout only, no column |

## Placement and behaviour

- `insert_after`: an existing fieldname; omitted = end of the form. The card shows the neighbours.
- `fetch_from: "link_field.source_field"` copies a value when the Link changes; `link_field` must be a Link on this
  DocType and `source_field` must exist on its target. `fetch_if_empty: 1` keeps a value the user typed.
- `depends_on` (show), `mandatory_depends_on`, `read_only_depends_on`:
  - a fieldname: true when that field has a value (`depends_on: "customer"`);
  - `eval:` JavaScript on `doc` (the document, or the row inside a child table) and `parent` (the form's document):
    `eval:doc.status=="Open"`, `eval:doc.grand_total > 1000`, `eval:parent.customer_group=="Wholesale"`.
  - Client-side only: it hides or requires on the screen, it does not protect imports or the API.
- `reqd: 1` on a DocType that already has documents: each old document asks for it on its next save. The check reports
  how many. Prefer `mandatory_depends_on` or a default.
- `hidden: 1` together with `reqd: 1` and no `default` is refused by Frappe.
- `unique: 1` adds a unique index; existing duplicates make it fail (the check counts them).
- `in_list_view` / `in_standard_filter`: list column / sidebar filter. Not for Table, HTML, breaks, Button, Image.
- `allow_on_submit: 1`: editable after submit. `no_copy: 1`: not copied on duplicate. `print_hide: 1`: not printed.
- `permlevel`: 0 is everyone who can read the DocType. A higher level is visible only to roles with a permission row
  at that level; if no role has one, nobody sees the field (the check warns).
- `translatable: 1` only for text types.

## Update and remove

- `update`: change properties of an existing **custom** field. Standard fields are changed with
  `customize.request_properties`. Changing `fieldtype` is limited to compatible groups (Currency ↔ Float ↔ Percent,
  Data ↔ Small Text/Text/Select, Text ↔ Text Editor/Long Text…).
- `remove`: deletes the Custom Field. A field created by Administrator can only be removed by Administrator.
  The database column stays (orphan) with its data, so undo brings it back; nothing is lost silently.
- Adding a field runs DDL (`ALTER TABLE`), which commits in MariaDB: there is no transactional rollback, so a failed
  multi-field request removes the fields it created before the failure.
- MariaDB rows are limited to ~65,535 bytes; DocTypes with very many Data fields can hit it. Prefer Small Text for long
  free text.

## Example

```json
{"doctype": "Sales Invoice", "operation": "add", "reason": "Group invoices by delivery area",
 "fields": [{"fieldname": "custom_delivery_zone", "label": "منطقة التوصيل", "fieldtype": "Select",
             "options": "\nNorth\nSouth\nEast", "insert_after": "customer", "in_standard_filter": 1}]}
```
