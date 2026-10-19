# Custom fields (customize.request_field) — Frappe v16

Verified against `frappe/custom/doctype/custom_field/custom_field.py`, `core/doctype/doctype/doctype.py`
(`validate_fields`), `model/__init__.py`, `model/meta.py` and `public/js/frappe/form/layout.js` (Frappe 16.28).
Every example below passes `customize.check` (kind `custom_field`) on a live site.

Arguments: `doctype`, `operation` (`add` | `update` | `remove`), `fields` (1–10 objects), `reason`. Field keys
accepted: `fieldname label fieldtype options insert_after default description reqd hidden read_only bold
in_list_view in_standard_filter allow_on_submit no_copy print_hide unique translatable fetch_from fetch_if_empty
depends_on mandatory_depends_on read_only_depends_on permlevel precision length`. Unknown keys are refused, not
ignored.

## 1. Which DocTypes

- Standard DocTypes only. Not core DocTypes (`core_doctypes_list`: DocType, DocField, DocPerm, User, Role, Has
  Role, Page, Module Def, Print Format, Report, Customize Form, Property Setter, Custom Field, Client Script …), not
  Single DocTypes (settings), not custom DocTypes (`custom = 1`: those are edited as a DocType).
- Protected DocTypes (code, access, `… Settings`) are refused too. `customize.inspect` says `customizable: false`
  with the reason.

## 2. Fieldname rules

- Send an English, ASCII `snake_case` fieldname matching `^[a-z][a-z0-9_]{1,60}$`; the tool adds `custom_` when
  missing. Labels are in the user's language.
- Why: Frappe itself only adds `custom_` when it **derives** the name from the label, and keeps every letter
  (`c.isalpha()`), so an Arabic label becomes `custom_اسم_العميل`, and a label that is all symbols becomes
  `custom_` (seen on acc.dev: `Customer-custom_customer_`). **(docs differ: they suggest custom_ is always added)**
- Reserved, never use: `name owner creation modified modified_by docstatus idx doctype parent parentfield
  parenttype flags file_list`, `_user_tags _comments _assign _liked_by _seen`, any existing fieldname, and any method
  or property of the document class (`validate`, `meta`, `items`…; Frappe's `check_fieldname_conflicts`).
- The Custom Field record is named `<DocType>-<fieldname>`; for `update` / `remove` give the fieldname.
- A fieldname never changes after creation: to "rename", change the `label`.

## 3. Field types and what `options` means

| fieldtype | stores | `options` | tool |
|---|---|---|---|
| Data | varchar(140) (`length` makes it longer) | empty, or validation `Email`, `Phone`, `URL`, `Name`, `Barcode`, `IBAN` | yes |
| Small Text / Text / Long Text | text | — | yes |
| Text Editor | HTML text | — | yes |
| Markdown Editor, HTML Editor, Code | text | Code: language (`Python`, `JS`, `HTML`, `JSON`…) | no |
| Int / Float / Percent | numbers | — (`precision` "" = system default, "0"–"9" for Float/Percent) | yes |
| Currency | decimal | the **fieldname** holding the currency on the same form (`currency`), or `DocType:link_field:currency_field` (`Company:company:default_currency`); empty = default currency **(docs differ: they show `INR`)** | yes |
| Check | 0/1 | — (`default` "0" or "1"; a Check is never "missing", so `reqd` has no effect) | yes |
| Date / Datetime / Time / Duration | date/time | — (`default: "Today"` / `"now"`; Duration: `hide_days`, `hide_seconds`) | yes |
| Select | varchar | one option per line: `"\nOpen\nClosed"` (leading newline = blank allowed). Never comma-separated | yes |
| Link | name of a record | the target DocType, exact name; not a child table | yes |
| Dynamic Link | name of a record | the fieldname of a **Link to "DocType"** field on the same form | yes |
| Table | child rows | a child DocType (`istable = 1`) | yes |
| Table MultiSelect | child rows | a child DocType that has a Link field | yes |
| Attach / Attach Image | file URL | — (`image_field` of the DocType needs Attach Image) | yes |
| Phone | varchar | — | yes |
| Rating | decimal 0–1 | number of stars (3–10, default 5) | yes |
| Color | `#rrggbb` | — | yes |
| Read Only | shows a value | — (usually with `fetch_from`) | yes |
| HTML | nothing | the HTML to show | yes |
| Section Break / Column Break / Tab Break | nothing (layout) | — (`label` optional for breaks, `collapsible` on sections) | yes |
| Autocomplete, Barcode, Button, Geolocation, Icon, Image, JSON, Password, Signature, Heading, Fold | | | no |

## 4. Placement and behaviour

- `insert_after`: an existing fieldname (or a field added earlier in the same request); omitted = end of the form.
  The card shows the section and neighbours. Fields of one request are chained in order.
- `fetch_from: "link_field.source_field"` copies a value when the Link changes (also on save, server side);
  `link_field` must be a Link on this DocType and `source_field` a field of its target. Pair with `read_only: 1`
  for a mirror, or `fetch_if_empty: 1` to keep what the user typed.
- `depends_on` (show), `mandatory_depends_on`, `read_only_depends_on`, `collapsible_depends_on`:
  - a fieldname → true when that field has a value (`"customer"`; for a table: has rows);
  - `eval:` + a JavaScript expression with `doc` (the document, or the row inside a child table) and `parent` (the
    form's document): `eval:doc.status=="Open"`, `eval:doc.grand_total > 1000`, `eval:parent.customer_group=="Wholesale"`,
    `eval:in_list(["Open","Overdue"], doc.status)`, `eval:frappe.user.has_role("Accounts Manager")`;
  - use `==`/`!=`/`&&`/`||`/`!` (JavaScript), strings in quotes; no Python (`and`, `in`).
  - **Evaluated in the browser only.** `mandatory_depends_on` is not enforced on import/REST/server code
    (`BaseDocument._get_missing_mandatory_fields` checks only `reqd`). For a hard rule add a Before Save server script.
- `reqd: 1` on a DocType that already has records: each old record asks for it on its next save (the check counts
  them). Prefer `mandatory_depends_on`, a `default`, or a rule only for new documents.
- `hidden: 1` with `reqd: 1` and no `default` is refused.
- `unique: 1` adds a unique index; existing duplicates make it fail (counted in the check).
- `in_list_view` / `in_standard_filter`: list column / sidebar filter; not for breaks, HTML, Table, Button, Image.
- `allow_on_submit: 1` editable after submit; `no_copy: 1` not copied on Duplicate/amend; `print_hide: 1` not
  printed; `bold: 1`; `translatable: 1` only for text types.
- `permlevel`: 0 = everyone who can read the DocType. Level N is visible only to roles that have a permission row
  at level N (Role Permission Manager); with no such row nobody sees it (warning).
- `default`: a literal, `Today` (Date), `now` (Datetime/Time), or `__user` (the current user).

## 5. Update and remove

- `update` changes properties of an existing **custom** field. Standard fields change through
  `customize.request_properties`. A fieldtype change is limited to the compatible groups (see `properties`).
- `remove` deletes the Custom Field; a field created by Administrator can be removed only by Administrator. The
  database column and its data stay (orphan), so undo brings them back.
- Adding fields runs `ALTER TABLE` (MariaDB commits it): a failure in field n removes fields 1…n−1 of the request.
- MariaDB rows are limited to ~65,535 bytes: DocTypes with very many Data fields can hit it; use Small Text for long
  free text.

## 6. Examples (each passes `customize.check`)

### A Select with a dependent note (shown only when a zone is chosen)

```json customize kind=custom_field
{"doctype": "Sales Invoice", "operation": "add", "reason": "Group invoices by delivery area",
 "fields": [
   {"fieldname": "delivery_zone", "label": "منطقة التوصيل", "fieldtype": "Select",
    "options": "\nNorth\nSouth\nEast\nWest", "insert_after": "customer", "in_standard_filter": 1},
   {"fieldname": "delivery_notes", "label": "ملاحظات التوصيل", "fieldtype": "Small Text",
    "insert_after": "custom_delivery_zone", "depends_on": "eval:doc.custom_delivery_zone", "print_hide": 1}]}
```

### A read-only mirror of a linked value

```json customize kind=custom_field
{"doctype": "Sales Invoice", "operation": "add", "reason": "Show the customer's mobile on the invoice",
 "fields": [{"fieldname": "customer_mobile", "label": "جوال العميل", "fieldtype": "Data", "options": "Phone",
             "fetch_from": "customer.mobile_no", "read_only": 1, "insert_after": "customer_name", "no_copy": 1}]}
```

### A Check and a Currency that is required only when the Check is on

```json customize kind=custom_field
{"doctype": "Sales Order", "operation": "add", "reason": "Track deposits on orders",
 "fields": [
   {"fieldname": "deposit_required", "label": "Deposit required", "fieldtype": "Check", "default": "0",
    "insert_after": "delivery_date"},
   {"fieldname": "deposit_amount", "label": "Deposit amount", "fieldtype": "Currency", "options": "currency",
    "insert_after": "custom_deposit_required", "depends_on": "eval:doc.custom_deposit_required",
    "mandatory_depends_on": "eval:doc.custom_deposit_required", "allow_on_submit": 1}]}
```

## 7. Mistakes seen on real sites

| Seen | Fix |
|---|---|
| `Customer-custom_customer_` (acc.dev): fieldname derived from a label with no ASCII letters | send an English `fieldname` |
| Arabic fieldnames, spaces, `name`/`status`/`owner` as fieldnames | ASCII snake_case, check the DocType's fields first |
| Select options sent as `"A, B, C"` | `"A\nB\nC"` |
| `depends_on: "status == 'Open'"` (no `eval:`, Python syntax) | `eval:doc.status=="Open"` |
| `fetch_from: "customer_mobile"` (one part) or from a non-Link field | `link_field.source_field` with a Link field |
| Currency `options: "SAR"` | the currency **fieldname** (`currency`) |
| `reqd: 1` added to a DocType with thousands of records without telling the user | tell them, or use `mandatory_depends_on` |
| relying on `mandatory_depends_on` for imports/API | add a server script |

Sources: https://docs.frappe.io/framework/user/en/basics/doctypes/fieldtypes ,
https://docs.frappe.io/framework/user/en/basics/doctypes/docfield ,
https://docs.frappe.io/framework/user/en/basics/doctypes/customize — verified against the Frappe 16.28 source.
