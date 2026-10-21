# Client scripts (customize.request_client_script) — Frappe v16 Desk

Verified against `frappe/public/js/frappe/form/form.js`, `form/script_manager.js`, `list/*.js`,
`model/indicator.js` and `desk/form/meta.py` (Frappe 16.28). Every example below passes the checker
(`analyzers/js_code.py`: node + acorn).

Arguments: `operation` (`create` | `update` | `enable` | `disable`), `name` (needed for update/enable/disable; for
create the default is `<DocType> - <purpose>`), `doctype`, `view` (`Form` | `List`), `script` (≤ 20,000
characters), `purpose`. The tool adds a `// ai_agent: …` header line.

A client script runs in the browser of **everyone** who opens that form or list. It is convenience, not
protection: imports, REST, the mobile app and other scripts bypass it. A rule that must always hold is a server
script (Before Save). **(docs agree: "validations apply only in the browser form view")**

## 1. How Frappe loads them (matters for naming)

- All enabled **Form** client scripts of a DocType are concatenated, in creation order, into **one** function body
  (`new Function(__custom_js)()` in `script_manager.setup`). A syntax error, or a second top-level
  `function helper()` / `const x` with the same name in another script, breaks **every** client script of that
  DocType. Keep helpers inside the handler object (call them with `frm.trigger("my_helper")`) or give top-level
  helpers a unique prefix. **(docs differ: not documented)**
- `frappe.ui.form.on(doctype, {...})` **adds** handlers; several scripts may handle the same event, run in order.
- **List** scripts are evaluated after the DocType's own `*_list.js` (ERPNext's). `frappe.listview_settings[dt] =
  {...}` therefore **replaces** ERPNext's settings (its status indicators, buttons). Merge instead (example 6).
- The checker parses ECMAScript 2021 (acorn 7): optional chaining `?.`, `??`, `async/await`, arrow functions are
  fine; class fields and top-level `await` are not.

## 2. Form events — `frappe.ui.form.on("<DocType>", { event(frm) {...} })`

| Event | Fires | Typical use |
|---|---|---|
| `setup` | once per form instance, immediately (not awaited) | `frm.set_query` |
| `before_load` | before each load | rarely |
| `onload` | after the doc is loaded, before render | defaults for new docs |
| `refresh` | after every render (load, save, reload) | buttons, intro, toggles |
| `onload_post_render` | after the first render | focus, DOM tweaks |
| `<fieldname>` | the field's value changed (user or `set_value`) | dependent values |
| `validate` | Save and Submit (not "Update" after submit) | checks: `frappe.throw(...)` or `frappe.validated = false` stops the save |
| `before_save` | right after `validate` | last changes |
| `after_save` | after the server saved | messages, reload others |
| `before_submit` / `on_submit` | submit (after confirm) / after submit | |
| `before_cancel` / `after_cancel` | cancel | |
| `before_discard` / `after_discard` | discard of a draft (v15+) | |
| `before_workflow_action` / `after_workflow_action` | workflow buttons | |
| `timeline_refresh` | timeline rendered | |

Order on open: `setup` → `before_load` → `onload` → `refresh` → `onload_post_render`. On Save: `validate` →
`before_save` → server → `after_save` → `refresh`. A handler that **returns a Promise is awaited** before the next
one, so an `async validate(frm)` can read from the server and still stop the save.

### Child-table events

They use the **child DocType** name and receive `(frm, cdt, cdn)`: `cdt` = child DocType, `cdn` = row name, the row
is `locals[cdt][cdn]` (or `frappe.get_doc(cdt, cdn)`).

| Event (on the child DocType) | Fires |
|---|---|
| `<child_fieldname>(frm, cdt, cdn)` | a field of the row changed |
| `form_render(frm, cdt, cdn)` | the row opened as a form |

| Event (on the **parent** DocType, named after the Table field) | Fires |
|---|---|
| `<table>_add(frm, cdt, cdn)` | a row was added (`items_add`) |
| `before_<table>_remove(frm, cdt, cdn)` / `<table>_remove(frm)` | before / after a row removal |
| `<table>_move(frm, cdt, cdn)` | a row was dragged |
| `<table>_on_form_rendered(frm)` | a row opened as a form |

The checker accepts `frappe.ui.form.on` only for the requested DocType and its own child tables.

## 3. Form API

| Call | Does |
|---|---|
| `frm.doc.field`, `frm.doc.items` | read values (rows are plain objects) |
| `frm.set_value("field", value)` / `frm.set_value({a: 1, b: 2})` | set and fire change events; returns a Promise |
| `frm.set_df_property("field", "read_only" \| "hidden" \| "reqd" \| "label" \| "description" \| "options", value)` | change a property on this form only |
| `frm.set_df_property("items", "read_only", 1, frm.doc.name, "rate", row.name)` | the same for one child cell |
| `frm.toggle_display(fields, show)`, `frm.toggle_reqd(fields, reqd)`, `frm.toggle_enable(fields, editable)` | quick toggles; `fields` is a name or an array |
| `frm.set_query("link", () => ({ filters: { disabled: 0 } }))` | filter a Link (call in `setup`) |
| `frm.set_query("item_code", "items", (doc, cdt, cdn) => ({ filters: {...} }))` | filter a Link inside a child table |
| `frm.add_custom_button(__("Label"), () => {...}, __("Group"))` | a button (call in `refresh`); `frm.remove_custom_button(label, group)`, `frm.clear_custom_buttons()` |
| `frm.add_child("items", {item_code: "X", qty: 1})` then `frm.refresh_field("items")` | add a row |
| `frm.clear_table("items")` | remove all rows |
| `frm.refresh_field("field")`, `frm.refresh()`, `frm.reload_doc()` | redraw / reload from server |
| `frm.is_new()`, `frm.is_dirty()`, `frm.doc.docstatus` (0, 1, 2) | state |
| `frm.save()`, `frm.save("Submit")` | save from code (returns a Promise) |
| `frm.set_intro(__("text"), "blue")`, `frm.dashboard.set_headline(__("text"))` | messages on the form |
| `frm.trigger("event_name")` | run your own handler by name |
| `frm.call("method", args)` | a whitelisted **controller** method of the document |
| `frm.get_sum("items", "qty")` | sum a column |

Other APIs: `frappe.model.set_value(cdt, cdn, "field", value)` (child row; returns a Promise),
`frappe.db.get_value(doctype, name_or_filters, field_or_list)` → `Promise<{message: {field: value}}>`,
`frappe.db.get_list(doctype, {filters, fields, limit})`, `frappe.db.exists(doctype, name)`,
`frappe.db.count(doctype, {filters})`, `frappe.xcall(method, args)` → `Promise<message>`,
`frappe.call({method, args}).then((r) => r.message)`, `frappe.msgprint(__("text"))`,
`frappe.show_alert({message: __("Saved"), indicator: "green"})`, `frappe.throw(__("text"))`,
`frappe.confirm(__("Sure?"), () => {...})`, `frappe.prompt(fields, (values) => {...})`, `__("text")` (translate),
`flt(v, precision)`, `cint(v)`, `format_currency(v, currency)`, `frappe.datetime.get_today()`,
`frappe.datetime.add_days(d, n)`, `frappe.user.has_role("Accounts Manager")`, `frappe.session.user`,
`frappe.defaults.get_user_default("Company")`, `frappe.set_route("Form", doctype, name)`.

Everything that reads from the server is asynchronous: use `.then(...)` or `async`/`await`; the value is never there
on the next line. Every `frappe.call`/`frappe.xcall` method must exist when the request is checked: a missing one is
**critical**, so request the API server script first and the client script after it is approved.

## 4. List scripts (`view: "List"`) — `frappe.listview_settings["<DocType>"]`

| Key | Meaning |
|---|---|
| `add_fields: ["status", "due_date"]` | extra fields loaded per row (for indicators/formatters) |
| `filters: [["status", "=", "Open"]]` | default filters when the list opens |
| `hide_name_column: true`, `hide_name_filter: true` | hide the ID column / filter |
| `onload(listview)` | once: `listview.page.add_inner_button(label, fn)`, `listview.filter_area.add([[dt, field, op, value]])` |
| `before_render()`, `refresh(listview)` | before each render / after each refresh |
| `get_indicator(doc)` → `[label, color, "field,=,value"]` | the status pill; drafts only with `has_indicator_for_draft: true` |
| `formatters: { field(value, df, doc) { return html; } }` | display of one column |
| `button: { show(doc), get_label(), get_description(doc), action(doc) }` | a button on every row |
| `primary_action()`, `get_form_link(doc)` | override "New" / the row link |

The checker requires the script to assign `frappe.listview_settings["<doctype>"]` for the requested DocType.

## 5. Examples (each passes the checker)

### 1. PO number required for wholesale customers (helpers inside the handler object)

```js client-script doctype="Sales Invoice" view=Form
frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        frm.trigger("ai_po_required");
    },
    customer(frm) {
        frm.trigger("ai_po_required");
    },
    ai_po_required(frm) {
        if (!frm.doc.customer) {
            frm.toggle_reqd("po_no", false);
            return;
        }
        return frappe.db.get_value("Customer", frm.doc.customer, "customer_group").then(({ message }) => {
            frm.toggle_reqd("po_no", !!message && message.customer_group === "Wholesale");
        });
    },
});
```

### 2. Filter Link choices, in the form and in the items table

```js client-script doctype="Sales Invoice" view=Form
frappe.ui.form.on("Sales Invoice", {
    setup(frm) {
        frm.set_query("customer", () => ({ filters: { disabled: 0 } }));
        frm.set_query("item_code", "items", (doc, cdt, cdn) => ({
            filters: { is_sales_item: 1, disabled: 0, has_variants: 0 },
        }));
    },
});
```

### 3. Child table: default warehouse on new rows, cap the discount per row

```js client-script doctype="Sales Invoice" view=Form
frappe.ui.form.on("Sales Invoice", {
    items_add(frm, cdt, cdn) {
        if (frm.doc.set_warehouse) {
            frappe.model.set_value(cdt, cdn, "warehouse", frm.doc.set_warehouse);
        }
    },
});

frappe.ui.form.on("Sales Invoice Item", {
    discount_percentage(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (flt(row.discount_percentage) > 20) {
            frappe.show_alert({ message: __("Row {0}: discount capped at 20%", [row.idx]), indicator: "orange" });
            frappe.model.set_value(cdt, cdn, "discount_percentage", 20);
        }
    },
});
```

### 4. A button that calls an API server script (`customer_outstanding` from server-scripts)

```js client-script doctype="Sales Invoice" view=Form
frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        if (frm.is_new() || !frm.doc.customer) return;
        frm.add_custom_button(__("Customer balance"), () => {
            frappe.xcall("customer_outstanding", { customer: frm.doc.customer }).then((data) => {
                frappe.msgprint({
                    title: __("Open invoices"),
                    message: __("{0} invoices, total {1}", [data.invoices.length, format_currency(data.total, frm.doc.currency)]),
                });
            });
        }, __("View"));
    },
});
```

### 5. An async check that can stop the save

```js client-script doctype="Sales Order" view=Form
frappe.ui.form.on("Sales Order", {
    async validate(frm) {
        if (!frm.doc.customer) return;
        const { message } = await frappe.db.get_value("Customer", frm.doc.customer, "is_frozen");
        if (message && message.is_frozen) {
            frappe.throw(__("Customer {0} is frozen", [frm.doc.customer]));
        }
    },
    refresh(frm) {
        frm.toggle_enable("delivery_date", frm.doc.docstatus === 0);
        frm.set_df_property("po_no", "description", __("Required for wholesale customers"));
    },
});
```

### 6. List: add a button and an indicator without losing ERPNext's settings

```js client-script doctype="Sales Invoice" view=List
const ai_si_settings = frappe.listview_settings["Sales Invoice"] || {};
const ai_si_base_onload = ai_si_settings.onload;

frappe.listview_settings["Sales Invoice"] = Object.assign(ai_si_settings, {
    add_fields: [...new Set([...(ai_si_settings.add_fields || []), "outstanding_amount", "due_date"])],
    onload(listview) {
        if (ai_si_base_onload) ai_si_base_onload(listview);
        listview.page.add_inner_button(__("Unpaid"), () => {
            listview.filter_area.add([["Sales Invoice", "outstanding_amount", ">", 0]]);
        });
    },
});
```

## 6. Refused or flagged by the checker

| Pattern | Level |
|---|---|
| syntax error (acorn, ECMAScript 2021) | critical, with line and column |
| `frappe.ui.form.on` for another DocType; no `frappe.ui.form.on` for this one; List script without `listview_settings["<doctype>"]` | critical |
| `eval`, `new Function`, `setTimeout/setInterval` with a string, `import()` | critical |
| `document.cookie`, `localStorage`, `sessionStorage`, `indexedDB`, `frappe.boot` as a whole, `frappe.csrf_token` | critical |
| `fetch`, `XMLHttpRequest`, `$.ajax/get/post`, `navigator.sendBeacon`, `WebSocket`, `EventSource` to anything but `/api/…` | critical |
| `window.location = …`, `location.assign/replace`, `window.open` to an absolute or computed URL | critical |
| `frappe.call/xcall` to a method that does not exist | critical |
| `frappe.call/xcall` to `frappe.client.delete/set_value/submit/cancel/insert/save…` | warning |
| `"<script"`, `"<iframe"`, `.innerHTML =` / `insertAdjacentHTML` with non-constant text | warning |
| more than 400 lines | warning |

Prefer a property (`request_properties`: hidden, reqd, read_only, label, depends_on) over a script that does the
same; prefer `frm.set_df_property` over DOM manipulation.

## 7. Mistakes seen on real sites

| Seen | Fix |
|---|---|
| `Please set the document name` (acc.dev, a Client Script created through the generic path) | use this tool; it names the script |
| emoji / Arabic text hard-coded in buttons and `frm.set_value("status", "قيد المعالجة")` on a Select | labels through `__()`; Select values must be one of its options (usually English) |
| two Form scripts on one DocType each declaring `function update_totals()` | one helper name per DocType; keep helpers in the handler object |
| `frappe.listview_settings["X"] = {...}` hid ERPNext's status colours | merge with `Object.assign(existing, {...})` |
| reading `frappe.db.get_value(...)` and using the result on the next line | `.then(...)` / `await` |
| a client-side "validation" expected to block imports | a Before Save server script |
| `cur_frm` in new code | use the `frm` argument |

Sources: https://docs.frappe.io/framework/user/en/desk/scripting/client-script ,
https://docs.frappe.io/framework/user/en/api/form , https://docs.frappe.io/framework/user/en/api/list —
verified against the Frappe 16.28 source on this bench.
