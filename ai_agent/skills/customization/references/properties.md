# Field and form properties (customize.request_properties) — Frappe v16

Arguments: `doctype`, `doctype_properties` `{property: value}`, `field_properties` `{fieldname: {property: value}}`,
`field_order` (optional full order, same set of fieldnames), `reason`. At most 40 properties per request.

The change runs **Customize Form itself** (`fetch_to_customize` → apply → `save_customization`), not a raw Property
Setter, so Frappe's own rules apply. Customize Form silently skips a refused property (a message, no error); the check
simulates every property first and reports a refusal as critical, and the verification re-reads the form afterwards.
Verified against `frappe/custom/doctype/customize_form/customize_form.py`.

## What cannot be customized

Core DocTypes (`core_doctypes_list`), Single DocTypes and custom DocTypes (`custom = 1`). `customize.inspect` says so.

## DocType properties (the full list Customize Form accepts)

`search_fields`, `title_field`, `image_field`, `sort_field`, `sort_order`, `default_print_format`, `hide_toolbar`,
`allow_copy`, `istable`, `quick_entry`, `queue_in_background`, `editable_grid`, `max_attachments`,
`make_attachments_public`, `protect_attached_files`, `track_changes`, `track_views`, `allow_auto_repeat`,
`allow_import`, `allow_bulk_edit`, `show_name_in_global_search`, `show_preview_popup`, `default_email_template`,
`email_append_to`, `subject_field`, `sender_field`, `naming_rule`, `autoname`, `show_title_field_in_link`,
`is_calendar_and_gantt`, `default_view`, `force_re_route_to_default_view`, `translated_doctype`, `grid_page_length`,
`rows_threshold_for_grid_search`.

The tool offers the safe subset: `title_field`, `search_fields`, `sort_field`, `sort_order`, `image_field`,
`show_title_field_in_link`, `quick_entry`, `track_changes`, `track_views`, `allow_copy`, `allow_import`,
`editable_grid`, `max_attachments`, `default_view`, `show_name_in_global_search`, `show_preview_popup`,
`default_print_format`. Naming (`naming_rule`, `autoname`) is not offered: it changes every new document.

## Field properties (the full list Customize Form accepts)

`idx`, `label`, `fieldtype`, `options`, `sort_options`, `fetch_from`, `fetch_if_empty`, `show_dashboard`, `permlevel`,
`width`, `print_width`, `alignment`, `non_negative`, `reqd`, `unique`, `ignore_user_permissions`, `in_list_view`,
`in_standard_filter`, `in_global_search`, `in_preview`, `bold`, `no_copy`, `ignore_xss_filter`, `hidden`,
`collapsible`, `collapsible_depends_on`, `print_hide`, `print_hide_if_no_value`, `report_hide`, `allow_on_submit`,
`translatable`, `mandatory_depends_on`, `read_only_depends_on`, `depends_on`, `description`, `default`, `precision`,
`read_only`, `length`, `columns`, `remember_last_selected_value`, `allow_bulk_edit`, `auto_repeat`,
`allow_in_quick_entry`, `hide_border`, `hide_days`, `hide_seconds`, `is_virtual`, `link_filters`, `placeholder`,
`button_color`, `mask`, `set_only_once`.

The tool does not offer `ignore_user_permissions`, `ignore_xss_filter` or `is_virtual` (they weaken protection).

## What Frappe refuses (allow_property_change), word for word

| Property | Refused when | Frappe's message |
|---|---|---|
| `reqd` | turning off a standard field that is mandatory in the DocType | Row {0}: Not allowed to disable Mandatory for standard fields |
| `read_only` | turning off a standard field that is read-only in the DocType | You cannot unset 'Read Only' for field {0} |
| `allow_on_submit` | turning on for a standard field that does not have it | Row {0}: Not allowed to enable Allow on Submit for standard fields |
| `options` | field type is not Read Only, HTML or Data | You can't set 'Options' for field {0} |
| `in_list_view` | field type has no value (breaks, HTML, Table, Table MultiSelect, Button, Image, Fold, Heading) | 'In List View' not allowed for type {0} in row {1} |
| `translatable` | field type does not support translation | You can't set 'Translatable' for field {0} |
| `is_virtual` | turning on for a standard field | You can't set standard field {0} as virtual |
| `fieldtype` | not inside one allowed group | Fieldtype cannot be changed from {0} to {1} in row {2} |
| `length` | shorter than data already stored | (checked on the data) |

`ALLOWED_FIELDTYPE_CHANGE`: (Currency, Float, Percent), (Small Text, Data), (Text, Data), (Text, Text Editor, Code,
Signature, HTML Editor), (Data, Select), (Text, Small Text, Long Text), (Text, Data, Barcode), (Code, Geolocation),
(Table, Table MultiSelect). `ALLOWED_OPTIONS_CHANGE`: Read Only, HTML, Data.

Also: `hidden: 1` with `reqd: 1` and no default is refused; `title_field`, `search_fields`, `sort_field` and
`image_field` must name existing fields (`image_field` an Attach Image field). `unique`, `length` and a larger
`precision` alter the table (DDL).

## Recipes

- **Column in the list**: `{"field_properties": {"po_no": {"in_list_view": 1, "columns": 2}}}`. `columns` is the
  width in grid units (1–10). The card shows the list columns after the change. Users who saved their own list
  settings keep them until they reset the list view.
- **Filter in the list sidebar**: `in_standard_filter: 1` (Link, Select, Data, Date, Check work best).
- **Document title**: `doctype_properties: {"title_field": "customer_name", "show_title_field_in_link": 1}`.
- **Search in Link fields**: `search_fields: "customer_name,mobile_no"` (comma separated fieldnames).
- **Default order**: `sort_field: "posting_date", sort_order: "DESC"`.
- **Quick entry**: `quick_entry: 1` on the DocType plus `allow_in_quick_entry: 1` on each field to show; mandatory
  fields always appear.
- **Track changes**: `track_changes: 1` keeps a Version for every save.
- **Rename a label**: `{"field_properties": {"remarks": {"label": "ملاحظات العميل"}}}` (the fieldname never changes).
- **Hide a field**: `hidden: 1` (refused for a mandatory field without default); show it again with `hidden: 0`.

## Undo

Restores the previous values; a Property Setter that did not exist before is deleted
(`delete_property_setter`). Undo is a new request with its own card.
