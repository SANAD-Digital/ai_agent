# Print formats and notifications (customize.request_print_format, customize.request_notification)

Verified against `frappe/printing/doctype/print_format/print_format.py`, `frappe/www/printview.py`,
`frappe/utils/jinja.py`, `frappe/utils/safe_exec.py` and `frappe/email/doctype/notification/notification.py`
(Frappe 16.28). Every example below passes the checker on a live site. Both tools need System Manager and their
own AI Settings switch; each request is checked, previewed on a real document and approved by a person.

Read what exists first: `customize.inspect` with `include: ["print_formats", "notifications"]`, then
`customize.get` (kind `print_format` / `notification`) for the full record.

## 1. Print Format (`customize.request_print_format`)

Use it for a **custom Jinja (HTML) layout** of one DocType ("a simpler invoice", "an Arabic delivery note",
"make X the default print"). Not for: Print Format Builder layouts (drag-and-drop, edit them in ERPNext), report
print formats, or changing a **standard** format (create a copy under a new name instead).

| Argument | Notes |
|---|---|
| `operation` | `create`, `update`, `disable`, `enable`, `set_default`, `unset_default` |
| `name` | New for create (3-140 characters). Existing for the others |
| `doc_type` | create only; the DocType cannot change later |
| `html` | The Jinja template (required for create). Stored with a first line `{# ai_agent: purpose \| approval \| requester \| approver \| date #}` (invisible in print) |
| `css` | Optional CSS; must not contain `</style>` |
| `font_size` (6-40), `margin_top/bottom/left/right` (mm, 0-100), `align_labels_right` (0/1) | Page settings |
| `default_print_format` | With create/update/enable: `1` makes it the DocType's default (a Property Setter, like Customize Form), `0` removes it. A custom DocType's default is set in the DocType itself: refused here |
| `sample_document` | The document the preview renders on; default: the latest one you can read (for submittable DocTypes: submitted, or drafts when Print Settings allow them) |
| `purpose` | One line for the card |

Undo: create → disable (and the old default back); update → the previous template and settings; disable ↔ enable;
set_default → unset (and the previous default back); unset_default → set_default.

### Template context

`doc` (the Document: `doc.items`, `doc.get_formatted("grand_total")`, `row.get_formatted("amount", doc)`),
`meta`, `letter_head`, `footer`, `no_letterhead`, `print_settings`, `layout`, `_()`, and the `frappe` namespace
(`frappe.utils.formatdate`, `frappe.format_value`, `frappe.get_url`, `frappe.get_list` — which applies the
viewer's permissions).

```json customize kind=print_format
{"operation": "create", "name": "Sales Invoice - Simple (AI)", "doc_type": "Sales Invoice",
 "purpose": "One-page invoice without item images", "font_size": 12,
 "html": "<div class=\"print-format\" dir=\"auto\">\n  <h2>{{ _(\"Invoice\") }} {{ doc.name }}</h2>\n  <p>{{ _(\"Customer\") }}: {{ doc.customer_name or doc.customer }} · {{ frappe.utils.formatdate(doc.posting_date) }}</p>\n  {% if doc.items %}\n  <table class=\"table table-bordered\">\n    <tr><th>#</th><th>{{ _(\"Item\") }}</th><th>{{ _(\"Qty\") }}</th><th>{{ _(\"Amount\") }}</th></tr>\n    {% for row in doc.items %}\n    <tr><td>{{ row.idx }}</td><td>{{ row.item_name }}</td><td>{{ row.qty }}</td><td>{{ row.get_formatted(\"amount\", doc) }}</td></tr>\n    {% endfor %}\n  </table>\n  {% endif %}\n  <p><b>{{ _(\"Grand Total\") }}: {{ doc.get_formatted(\"grand_total\") }}</b></p>\n</div>"}
```

### Checks you can hit (and the fix)

| Code | Level | Fix |
|---|---|---|
| `jinja.syntax` | critical, with line | Close every `{% if %}`/`{% for %}`; the message is Jinja's own |
| `jinja.write` | critical | Templates run on the server with Frappe's full script namespace: never call `sendmail`, `set_value`, `commit`, `save`, `insert`, `db_set`, `delete_doc`, `enqueue`, `make_post_request`, `get_print`, `render_template`… Print only |
| `jinja.attr_filter`, `jinja.dynamic_access`, `jinja.dunder` | critical | No `\|attr(...)`, no `frappe[...]` with a computed key, no `.__x` — write plain `frappe.utils.x` |
| `print.render` | critical | The render on the sample document failed (message = the last error line). Guard empty tables (`{% if doc.items %}`), missing values (`doc.x or ""`), division by zero |
| `print.permission` | critical | The requester cannot read/print the sample document |
| `print.sample` | critical / warning | `sample_document` does not exist or is not readable (critical); no readable document at all (warning: compiled, not rendered) |
| `print.doctype` | critical | Protected/core/settings DocType, a child table, or a DocType the requester cannot read |
| `print.name_taken` | critical | Use `update`, or another name |
| `print.css` | critical | Remove `</style>` from css |
| `print.validate` | critical | Frappe's own `PrintFormat.validate` refused it |
| `print.default_custom`, `print.default_disabled`, `print.not_default` | critical | Custom DocType default; enable before making default; unset only the current default |
| `jinja.unpermitted_read` | warning | `frappe.db.*`, `frappe.get_all`, `frappe.get_doc`, `frappe.get_value` read **without permission checks**: everyone who prints sees that data. Prefer fields of `doc` or `frappe.get_list` |
| `jinja.sql` | critical | `frappe.db.sql` in a template: read through the document's fields or `frappe.get_list` |
| `jinja.protected_doctype` / `jinja.dynamic_doctype` | critical | a read of identity, access or settings DocTypes (Has Role, User Permission, OAuth…, *Settings, AI…), or a DocType named by a computed value. Company, User (full name), Letter Head stay readable |
| `jinja.script` | warning | `<script>`, `on*=` handlers or `javascript:` run in every browser that opens the print |
| `jinja.external_url` | warning | `src`/`href`/`url()` to another site: readers' browsers contact it. Use files uploaded to this site |
| `print.disable_default` | warning | Disabling the current default: printing falls back to the DocType's own default until undone |

The card shows the template (and a diff on update), the first 6000 characters of the rendered HTML, and the
document used.

## 2. Notification (`customize.request_notification`)

Use it for "email/notify X when Y happens". Channels: **Email** and **System Notification** (bell icon). SMS and
Slack only when the site already has them configured (SMS Settings gateway / a Slack Webhook URL record).

| Argument | Notes |
|---|---|
| `operation` | `create`, `update`, `disable`, `enable`. A standard notification can only be disabled |
| `name` | New for create |
| `document_type` | create only |
| `event` | `New`, `Save`, `Submit`, `Cancel`, `Value Change` (+ `value_changed`), `Method` (+ `method`, e.g. `on_update_after_submit`), `Days Before`/`Days After` (+ `date_changed`, `days_in_advance`; daily scheduler), `Minutes Before`/`Minutes After` (+ `datetime_changed`, `minutes_offset` ≥ 10). `Custom` is refused (fires only from code) |
| `channel` | default `Email` |
| `condition` | One Python expression; names: `doc`, `nowdate()`, `frappe` (e.g. `frappe.utils.date_diff`). Builtins are limited (`int`, `float`, `round`…), no lambda, no `:=` |
| `subject` | Required for Email / System Notification / Slack; Jinja when it contains `{` |
| `message` | Jinja with `doc`, `alert`, `comments`, `nowdate`, `frappe`, `_()` |
| `recipients` | List of rows. Update replaces the whole list. Row keys: `receiver_by_role` (every enabled user with the role), `receiver_by_document_field` (`owner`, a User link such as `allocated_to`, a Data field with options Email such as `contact_email`; a child-table field as `"field,child_table"`), `cc` / `bcc` (addresses or Jinja), `condition` (this row only when true) |
| `allow_external` | `true` only **after the user confirmed** sending to the listed cc/bcc addresses that are not users of this site |
| `attach_print` + `print_format` | Email only; the print format must exist, be enabled and belong to `document_type` |
| `send_system_notification`, `send_to_all_assignees` | 0/1 |
| `sample_document` | The document for the dry run; default: the latest you can read |

Never send `set_property_after_alert`: a notification that writes to the document is refused.

```json customize kind=notification
{"operation": "create", "name": "Overdue Sales Invoice (AI)", "document_type": "Sales Invoice",
 "event": "Days After", "date_changed": "due_date", "days_in_advance": 1, "channel": "Email",
 "condition": "doc.docstatus == 1 and doc.outstanding_amount > 0",
 "subject": "{{ doc.name }} is overdue",
 "message": "<p>{{ _(\"Invoice\") }} {{ doc.name }} ({{ doc.customer_name }}) is overdue by {{ frappe.utils.date_diff(nowdate(), doc.due_date) }} days.</p>\n<p>{{ _(\"Outstanding\") }}: {{ doc.get_formatted(\"outstanding_amount\") }}</p>",
 "recipients": [{"receiver_by_role": "Accounts Manager"}, {"receiver_by_document_field": "owner"}],
 "purpose": "Remind accounts a day after the due date"}
```

### Checks you can hit (and the fix)

| Code | Level | Fix |
|---|---|---|
| `notification.set_property` | critical | Remove `set_property_after_alert`; change documents with a workflow or server script instead |
| `condition.syntax`, `condition.write`, `condition.dunder`, `condition.dynamic_access` | critical | One expression that only *tests* values; no calls that write/send |
| `notification.condition` | critical | The condition raised on the sample document (e.g. comparing `None`); guard with `(doc.x or 0) > 1` |
| `notification.validate` | critical | Frappe's own `Notification.validate` refused it (its message is shown) |
| `jinja.syntax`, `jinja.write`, `jinja.attr_filter`… | critical | As for print formats, for `subject`, `message` and Jinja `cc` |
| `notification.render` | critical | Subject/message failed on the sample document |
| `notification.recipients` | critical | Nobody would receive it, or resolving recipients failed |
| `notification.role` | critical | The role does not exist (`schema` tools list roles) |
| `notification.field` | critical | Field is not an email/User field for this channel. A Customer link is not an address: use `contact_email`. System Notification needs `owner` or a User link |
| `external.confirm` | critical | cc/bcc addresses are not site users: ask the user, then resend with `allow_external: true` |
| `notification.channel` | critical | SMS/Slack not configured; `slack_webhook_url` without Slack |
| `notification.event` | critical | Missing `date_changed` / `datetime_changed` / `value_changed` / `method`, `minutes_offset` < 10, Submit/Cancel on a non-submittable DocType |
| `notification.print` | critical | Missing/disabled/foreign print format, or attach_print with a non-Email channel |
| `notification.doctype`, `notification.subject`, `notification.message`, `notification.cc` | critical | Allowed DocType; subject/message present; cc only for Email/System Notification |
| `external.address`, `external.document_field`, `external.computed` | warning | Leaves the company: confirmed addresses, addresses stored on each document, or cc computed by Jinja |
| `notification.mass` | warning | A role with more than 50 users: everyone gets every message |
| `notification.print_draft` | warning | Draft documents cannot be printed (Print Settings): the email fails for drafts |
| `jinja.unpermitted_read` | warning | The message reads data without permission checks and mails it |
| `jinja.sql` / `jinja.protected_doctype` / `jinja.dynamic_doctype` | critical | as for print formats: the result is mailed |

The card shows whether the condition holds on the sample document, the rendered subject and message, the resolved
recipients (to/cc/bcc), and the impact: who receives it and roughly how many messages a day (documents created per
day over the last 30 days × recipients; `Save` fires on every save, so more).

## 3. Workflow (reference)

States map to `docstatus` (0 draft, 1 submitted, 2 cancelled); transitions name a role and an action; a state may set
a field (`update_field`/`update_value`). Workflows replace the Submit/Cancel buttons for that DocType, so adding one
changes daily work for every user: always an administrator decision.

Sources: https://docs.frappe.io/framework/user/en/desk/printing , https://docs.frappe.io/framework/user/en/api/jinja —
verified against the Frappe 16.28 source.
