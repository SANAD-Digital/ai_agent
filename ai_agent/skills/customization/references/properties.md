# Field and form properties (customize.request_properties) — Frappe v16

Verified against `frappe/custom/doctype/customize_form/customize_form.py` (Frappe 16.53). Every example below
passes `customize.check` (kind `properties`) on a live site.

Arguments: `doctype`, `doctype_properties` `{property: value}`, `field_properties` `{fieldname: {property: value}}`,
`field_order` (optional: the complete new order, same set of fieldnames), `reason`. At most 40 properties per
request. Works for standard **and** custom fields of the DocType (custom fields can also change through
`customize.request_field` operation `update`).

The change runs **Customize Form itself** (`fetch_to_customize` → apply → `save_customization`), not a raw
Property Setter, so Frappe's own rules apply. Customize Form silently **skips** a refused property (it shows a
message and saves the rest); the check replays every property first and reports a refusal as critical, and the
verification re-reads the form afterwards.

## 1. What cannot be customized

Core DocTypes (`core_doctypes_list`), Single DocTypes, custom DocTypes (`custom = 1`) and protected DocTypes.
`customize.inspect` says so.

## 2. DocType properties

Everything Customize Form accepts (`doctype_properties` in the source):
`search_fields`, `title_field`, `image_field`, `sort_field`, `sort_order`, `default_print_format`, `hide_toolbar`,
`allow_copy`, `istable`, `quick_entry`, `queue_in_background`, `editable_grid`, `max_attachments`,
`make_attachments_public`, `protect_attached_files`, `track_changes`, `track_views`, `allow_auto_repeat`,
`allow_import`, `allow_bulk_edit`, `show_name_in_global_search`, `show_preview_popup`, `default_email_template`,
`email_append_to`, `subject_field`, `sender_field`, `naming_rule`, `autoname`, `show_title_field_in_link`,
`is_calendar_and_gantt`, `default_view`, `force_re_route_to_default_view`, `translated_doctype`, `grid_page_length`,
`rows_threshold_for_grid_search`.

Offered by this tool (the safe subset): `title_field`, `search_fields`, `sort_field`, `sort_order` (`ASC`/`DESC`),
`image_field` (an Attach Image field), `show_title_field_in_link`, `quick_entry`, `track_changes`, `track_views`,
`allow_copy`, `allow_import`, `editable_grid`, `max_attachments`, `default_view` (`List`, `Report`, `Dashboard`,
`Kanban`, `Calendar`…), `show_name_in_global_search`, `show_preview_popup`, `default_print_format` (an existing
Print Format). Not offered: naming (`naming_rule`, `autoname`: changes every new document's name), email
settings, `istable`, `hide_toolbar`.

## 3. Field properties

Everything Customize Form accepts (`docfield_properties`):
`idx`, `label`, `fieldtype`, `options`, `sort_options`, `fetch_from`, `fetch_if_empty`, `show_dashboard`, `permlevel`,
`width`, `print_width`, `alignment`, `non_negative`, `reqd`, `unique`, `ignore_user_permissions`, `in_list_view`,
`in_standard_filter`, `in_global_search`, `in_preview`, `bold`, `no_copy`, `ignore_xss_filter`, `hidden`,
`collapsible`, `collapsible_depends_on`, `print_hide`, `print_hide_if_no_value`, `report_hide`, `allow_on_submit`,
`translatable`, `mandatory_depends_on`, `read_only_depends_on`, `depends_on`, `description`, `default`, `precision`,
`read_only`, `length`, `columns`, `remember_last_selected_value`, `allow_bulk_edit`, `auto_repeat`,
`allow_in_quick_entry`, `hide_border`, `hide_days`, `hide_seconds`, `is_virtual`, `link_filters`, `placeholder`,
`button_color`, `mask`, `set_only_once`, `in_import_template` (Frappe 16.53: include the field in the Data Import
template).

Offered by this tool: `label hidden reqd read_only bold in_list_view in_standard_filter in_global_search in_preview
allow_on_submit default options description depends_on mandatory_depends_on read_only_depends_on collapsible
collapsible_depends_on print_hide print_hide_if_no_value report_hide no_copy permlevel precision columns width
fetch_from fetch_if_empty allow_in_quick_entry placeholder unique length link_filters remember_last_selected_value
set_only_once non_negative mask`. Not offered: `ignore_user_permissions`, `ignore_xss_filter`, `is_virtual` (they
weaken protection), `fieldtype` (custom fields only, through `request_field`), `idx` (use `field_order`).

The `depends_on` family uses the same syntax as custom fields (`eval:doc.x=="y"`, see `fields`), and is enforced in
the browser only.

## 4. What Frappe refuses on a **standard** field (`allow_property_change`), word for word

| Property | Refused when | Frappe's message |
|---|---|---|
| `reqd` | turning off a field that is mandatory in the DocType | Row {0}: Not allowed to disable Mandatory for standard fields |
| `read_only` | turning off a field that is read-only in the DocType | You cannot unset 'Read Only' for field {0} |
| `allow_on_submit` | turning on for a field that does not have it in the DocType | Row {0}: Not allowed to enable Allow on Submit for standard fields |
| `options` | the field type is not Read Only, HTML or Data — **so the options of a standard Select or Link cannot change** | You can't set 'Options' for field {0} |
| `in_list_view` | field types without a value (breaks, HTML, Table, Table MultiSelect, Button, Image, Fold, Heading) | 'In List View' not allowed for type {0} in row {1} |
| `translatable` | the type does not support translation | You can't set 'Translatable' for field {0} |
| `is_virtual` | turning on for a standard field | You can't set standard field {0} as virtual |
| `fieldtype` | outside the allowed groups below | Fieldtype cannot be changed from {0} to {1} in row {2} |
| `length` | shorter than data already stored | (checked on the data) |

`ALLOWED_FIELDTYPE_CHANGE`: (Currency, Float, Percent), (Small Text, Data), (Text, Data), (Text, Text Editor, Code,
Signature, HTML Editor), (Data, Select), (Text, Small Text, Long Text), (Text, Data, Barcode), (Code, Geolocation),
(Table, Table MultiSelect). `ALLOWED_OPTIONS_CHANGE`: Read Only, HTML, Data.

To offer other choices than a standard Select allows: add a custom Select field (and hide the standard one if it is
not mandatory), or ask an administrator. **(docs: not documented; the Customize Form page lists no per-property limits)**

Also: `hidden: 1` with `reqd: 1` and no `default` is refused; `title_field`, `search_fields`, `sort_field` and
`image_field` must name existing fields; `unique`, `length` and a larger `precision` alter the table (DDL).

## 5. Recipes and examples (each passes `customize.check`)

- **List column**: `in_list_view: 1` plus `columns` (width in grid units, 1–10). Users who saved their own list
  settings keep them until they reset the list view.
- **Sidebar filter**: `in_standard_filter: 1` (Link, Select, Data, Date, Check work best).
- **Document title / link search / default order / quick entry / change history**: see below.
- **Rename a label**: the fieldname never changes. **Hide**: `hidden: 1` (refused for a mandatory field without
  default); show again with `hidden: 0`.

### Customer PO number as a list column and filter, with a clearer label

```json customize kind=properties
{"doctype": "Sales Invoice", "reason": "See and filter the customer's PO number in the invoice list",
 "field_properties": {"po_no": {"in_list_view": 1, "columns": 2, "in_standard_filter": 1, "label": "رقم أمر شراء العميل"}}}
```

### Title, link search, default sort and change history on Customer

```json customize kind=properties
{"doctype": "Customer", "reason": "Find customers by name or mobile and keep a change history",
 "doctype_properties": {"title_field": "customer_name", "show_title_field_in_link": 1,
                        "search_fields": "customer_name,mobile_no", "sort_field": "modified", "sort_order": "DESC",
                        "track_changes": 1}}
```

### Quick entry with chosen fields

```json customize kind=properties
{"doctype": "Customer", "reason": "Create customers from a short dialog",
 "doctype_properties": {"quick_entry": 1},
 "field_properties": {"customer_group": {"allow_in_quick_entry": 1}, "territory": {"allow_in_quick_entry": 1},
                      "mobile_no": {"allow_in_quick_entry": 1}}}
```

### Show a field only for submitted documents, read-only after submit

```json customize kind=properties
{"doctype": "Sales Invoice", "reason": "Remarks are written before submit and frozen after it",
 "field_properties": {"remarks": {"read_only_depends_on": "eval:doc.docstatus==1", "bold": 1}}}
```

## 6. Undo

Restores the previous values; a Property Setter that did not exist before is deleted. Undo is a new request with
its own card.

## 7. Mistakes seen

| Seen | Fix |
|---|---|
| changing `options` of a standard Select (e.g. Customer `customer_type`) | refused by Frappe: add a custom Select instead |
| `reqd: 0` on a standard mandatory field | refused: it cannot be made optional |
| `hidden: 1` on a mandatory field without default | refused: give a `default` first, or keep it visible |
| `fieldtype` in `field_properties` | only for custom fields, through `customize.request_field` |
| `search_fields: ["a", "b"]` | a comma-separated string `"a,b"` |
| a list column that "did not appear" | the user has saved list settings: reset them (List → menu → Reset) |

Sources: https://docs.frappe.io/framework/user/en/basics/doctypes/customize ,
https://docs.frappe.io/framework/user/en/basics/doctypes/form_&_view_settings — verified against the Frappe 16.28
source (`customize_form.py`).
