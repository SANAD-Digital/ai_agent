# Print formats and notifications — later phase (not offered by these tools yet)

The customize tools cannot create Print Formats, Notifications or Workflows (`actions.request_*` refuses them too).
When a user asks: explain what it would look like, give the snippet, and say an administrator adds it in ERPNext
(Print Format Builder / Notification list). Verified against `frappe/www/printview.py`, `frappe/utils/jinja.py`
and `frappe/email/doctype/notification/notification.py` (Frappe 16.28); the examples below render / evaluate on a
live site.

## Print Format (Jinja, `print_format_type: "Jinja"`, `custom_format: 1`)

Context: `doc` (the Document), `meta`, `letter_head`, `footer`, `no_letterhead`, `print_settings`, `layout`, plus
the Jinja globals: `_()`, `frappe.*` (the same namespace as server scripts: `frappe.db.get_value`, `frappe.get_list`,
`frappe.format_value`, `frappe.utils.*`, `frappe.get_url`). On a site with `disable_render_safe_exec: 0` the
restricted set applies (`frappe.get_doc` returns a dict). **(docs list only part of these)**

- `doc.get_formatted("grand_total")` formats with currency and precision; rows: `row.get_formatted("rate", doc)`.
- Child rows: `{% for row in doc.items %}`; totals are already on `doc`.
- Use `{{ _("Label") }}` for translatable text and `dir="rtl"` for Arabic layouts.
- Errors show as `PrintFormatError: … line N` (seen on acc.dev: `list object has no element 0` — indexing an empty
  table; guard with `{% if doc.items %}`).

```jinja print-format doctype="Sales Invoice"
<div class="print-format" dir="auto">
  <h2>{{ _("Invoice") }} {{ doc.name }}</h2>
  <p>{{ _("Customer") }}: {{ doc.customer_name or doc.customer }} · {{ frappe.utils.formatdate(doc.posting_date) }}</p>
  {% if doc.items %}
  <table class="table table-bordered">
    <tr><th>#</th><th>{{ _("Item") }}</th><th>{{ _("Qty") }}</th><th>{{ _("Amount") }}</th></tr>
    {% for row in doc.items %}
    <tr><td>{{ row.idx }}</td><td>{{ row.item_name }}</td><td>{{ row.qty }}</td><td>{{ row.get_formatted("amount", doc) }}</td></tr>
    {% endfor %}
  </table>
  {% endif %}
  <p><b>{{ _("Grand Total") }}: {{ doc.get_formatted("grand_total") }}</b></p>
</div>
```

## Notification

Fields: `document_type`, `event` (`New`, `Save`, `Submit`, `Cancel`, `Days After`, `Days Before`, `Minutes After`,
`Minutes Before`, `Value Change`, `Method`, `Custom`), `channel` (`Email`, `Slack`, `System Notification`, `SMS`),
`condition`, `subject`, `message` (Jinja with `doc`), recipients (by field, role, owner, assignees), `date_changed` +
`days_in_advance` for the Days events, `value_changed` for Value Change.

`condition` is **one Python expression** evaluated by `frappe.safe_eval` with only `doc` (a dict-like Document),
`nowdate` and `frappe` (the server-script namespace) — builtins are limited to `int`, `float`, `round` (no `str`, `len`); no lambda,
no `:=`, no statements.

```python notification-condition doctype="Sales Invoice"
doc.docstatus == 1 and doc.outstanding_amount > 0 and frappe.utils.date_diff(nowdate(), doc.due_date) > 0
```

Subject and message are Jinja: `{{ doc.name }} is overdue by {{ frappe.utils.date_diff(nowdate(), doc.due_date) }} days`.

## Workflow (reference)

States map to `docstatus` (0 draft, 1 submitted, 2 cancelled); transitions name a role and an action; a state may set
a field (`update_field`/`update_value`). Workflows replace the Submit/Cancel buttons for that DocType, so adding one
changes daily work for every user: always an administrator decision.

Sources: https://docs.frappe.io/framework/user/en/desk/printing , https://docs.frappe.io/framework/user/en/api/jinja —
verified against the Frappe 16.28 source.
