# Web pages and web forms (customize.request_web) — the public website

Verified against `frappe/website/doctype/web_page/web_page.py`, `web_form/web_form.py`, `website/path_resolver.py`,
`website/router.py` and `website/page_renderers/*.py` (Frappe 16.28). Every example below passes the checks.

A Web Page or a Web Form is served to **anyone who opens its URL**, without signing in (a Web Form with
`login_required` asks for sign-in first). Mistakes here are visible to the public: defacement, phishing pages, or
strangers creating records. So:

- Everything is created **unpublished**. Publish only when the user asked for it: `published: true` (create/update) or
  `operation: "publish"`. The card then says "visible to anyone on the internet at <URL>" and the risk is high.
- Choose a route under a prefix of the user's own (`offers/…`, `forms/…`). The check refuses Desk and server paths
  (`desk`, `app`, `api`, `assets`, `files`, `private`…), every app page (`login`, `contact`, `me`, `update-password`…),
  route rules (`kb/…`, `profile`), redirects, and any route of another Web Page, Web Form or web-view document. A Web
  Form also owns everything under its route (`<route>/new`, `/list`, `/<name>`, `/<name>/edit`).

Arguments: `operation` (`create` | `update` | `publish` | `unpublish` (alias `disable`) | `delete` — delete only an
unpublished record), `doctype` (`Web Page` | `Web Form`), `name` (not for create: default is the title in
lower-case-with-dashes), `title`, `route`, `published`, `meta_title`, `meta_description`, `purpose`, `allow_scripts`.
Page only: `content_type` (`Rich Text` | `Markdown` | `HTML`), `content`, `css`, `javascript`, `show_title`,
`dynamic_template`. Form only: `doc_type`, `form_fields`, `login_required`, `allow_guest`, `allow_edit`,
`allow_multiple`, `show_list`, `apply_document_permissions`, `introduction_text`, `success_title`,
`success_message`, `success_url`, `button_label`, `client_script`, `custom_css`.

Update sends only what changes (`form_fields` replaces the whole list). Undo: create → delete, update → the previous
values, publish ↔ unpublish, delete → create (unpublished).

## 1. Web Page

```json customize kind=web
{"operation": "create", "doctype": "Web Page", "title": "Summer offer 2026", "route": "offers/summer-2026",
 "content_type": "HTML", "show_title": 1,
 "content": "<section dir=\"auto\"><h2>Summer offer</h2><p>20% off every service until 31 August.</p><p><a href=\"/contact\">Contact us</a></p></section>",
 "meta_description": "Summer offer 2026", "purpose": "summer campaign landing page"}
```

The card shows the rendered page as an anonymous visitor sees it (first part of the HTML), the external links, and
the URL. Prefer `HTML` or `Markdown` for designed content; `Rich Text` is cleaned by Frappe's HTML sanitizer on save.

### What the check refuses (critical) and how to fix it

| Check | Why | Fix |
|---|---|---|
| `html.script`, `html.event_handler` (`onclick=`, `onerror=`…), `html.javascript_url` (`href="javascript:…"`), `html.embed` (`<iframe>`, `<object>`, `<embed>`), `html.base`, `html.meta_refresh`, `html.form_external` (form posting to another site), `html.password` | Runs code in visitors' browsers, sends them elsewhere, or collects what they type: defacement/phishing patterns | Remove it. Use plain links and images. Only if the user explicitly wants active content, send `allow_scripts: true` (each becomes a high-risk warning the approver reads) |
| `web.javascript` | The page's `javascript` field runs for every visitor | Same: only with `allow_scripts: true`; the code then goes through the JavaScript analyzer |
| `css.script` | `expression()`, `javascript:` or `data:` URLs in CSS | Plain CSS only |
| `web.context_script` | `context_script` is Python run on the server for every view (`safe_exec`); never written by the assistant. A page that already has one can only be unpublished here | Show the data another way, or let an administrator do it in Desk |
| `jinja.data` | Content with `{{ }}` (or `dynamic_template`) is rendered as Jinja for anonymous visitors with the full server globals: `frappe.get_all`, `frappe.db`, `frappe.form_dict` (echoes the URL — XSS), `frappe` passed as a value, `doc.<method>()` | Use only formatting: `{{ frappe.utils.formatdate(...) }}`, `{{ frappe.format_value(...) }}`, `{{ _("Text") }}`. Write the numbers into the HTML instead of reading them |
| `jinja.syntax`, `jinja.illegal` (`.__`) | Frappe cannot render it | Fix the template |
| `web.route_taken`, `web.route` | The route belongs to something else (see above) | Pick a free route under the user's own prefix |
| `web.name_taken` | A record with that name exists | `operation: "update"`, or another title/name |
| `web.content_type` | Page Builder / Slideshow pages are built from blocks not reviewed here | Edit the blocks in Desk, or replace the content with `content_type` + `content` |
| `web.preview` | The page does not render | Read the error, fix the content |
| `roles.missing` | Requester and approver need System Manager **and** Website Manager | Ask an administrator |

Warnings: `web.published` (visible to the internet), `web.route_home` (the route is the site's home page),
`css.import`, and the `html.*` ones under `allow_scripts`. Info: `html.external_links` lists every link to another
site — mention them to the user.

## 2. Web Form

Frappe saves every submission with `ignore_permissions` (`web_form.py::accept`): whoever can open the form can create
records of `doc_type`, even without a role that allows it. `login_required` therefore defaults to **1**.

```json customize kind=web
{"operation": "create", "doctype": "Web Form", "title": "Support request", "route": "forms/support",
 "doc_type": "Issue", "login_required": true,
 "form_fields": [{"fieldname": "subject", "reqd": 1}, {"fieldname": "description"}],
 "introduction_text": "<p>Tell us what went wrong.</p>", "success_message": "Thank you, we will reply soon.",
 "purpose": "customers report problems from the portal"}
```

`{fieldname}` is enough per field: label, type and options come from the DocType. Layout rows have no fieldname:
`{"fieldtype": "Section Break", "label": "Details"}`. The preview shows the form header and introduction (the
fields themselves are drawn in the browser; their list is on the card).

| Check | Level | Fix |
|---|---|---|
| `form.doctype` | critical | `doc_type` must be a normal DocType: not core, Single, child table or a protected (code/access/settings) one |
| `form.create_permission` | critical | The requester must be able to create `doc_type` themselves |
| `form.field_missing`, `form.fieldtype`, `form.hidden_mandatory` | critical | Use fields of `doc_type`; types allowed in Web Form Field; a hidden mandatory field needs a `default` |
| `form.permlevel` | critical | The field is protected by field-level permissions; the form would bypass them. Leave it out |
| `form.guest` | critical | `login_required: false` lets the internet create records. Only when the user explicitly wants a public form: add `allow_guest: true` (it becomes a warning; `allow_edit` and `show_list` are forced to 0) |
| `form.guest_link` | critical | On a public form a Link field lists every record name of the linked DocType to anonymous visitors. Remove the field or use a Data/Select field |
| `js.*` (from `client_script`) | as in client scripts | See the client-scripts reference: no eval, no off-site requests, no cookies/storage |
| `form.mandatory_missing` | warning | `doc_type` requires fields the form does not ask for: add them, or submissions fail |
| `form.guest_sensitive`, `form.allow_multiple`, `form.signup_open`, `form.link_all_options`, `form.read_only`, `form.success_url` | warning | Tell the user what it means; change only if they agree |

## 3. Publish, unpublish, undo

```json
{"operation": "publish", "doctype": "Web Page", "name": "summer-offer-2026"}
{"operation": "unpublish", "doctype": "Web Form", "name": "support-request"}
```

Publishing re-runs every content check on what is saved (a page someone else wrote with scripts needs
`allow_scripts: true` to be published here). To take something off the internet quickly, unpublish: it is always
low risk. `customize.request_undo` reverses an executed change with the same checks.
