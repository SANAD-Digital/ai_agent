# Client scripts (customize.request_client_script) — Frappe v16 Desk

Arguments: `operation` (`create` | `update` | `enable` | `disable`), `name` (needed for update/enable/disable; for
create the default is `<DocType> - <purpose>`), `doctype`, `view` (`Form` | `List`), `script` (≤ 20,000 characters),
`purpose`. The tool adds a provenance header comment.

A client script runs in the browser of **everyone** who opens that form or list. It is convenience, not protection:
imports, the REST API and other scripts bypass it. A rule that must always hold is a server script (Before Save).

## Form scripts

```javascript
frappe.ui.form.on("Sales Invoice", {
    setup(frm) {},              // once per form instance: set_query belongs here
    onload(frm) {},             // document loaded
    refresh(frm) {},            // every render: buttons, indicators, toggles
    validate(frm) {},           // before save; frappe.throw(...) or `frappe.validated = false` stops it
    before_save(frm) {},
    after_save(frm) {},
    before_submit(frm) {}, on_submit(frm) {}, before_cancel(frm) {}, after_cancel(frm) {},
    customer(frm) {},           // a field changed: the event is the fieldname
    items_add(frm, cdt, cdn) {},     // a row added to the `items` table; `items_remove(frm)` when removed
});
```

Child table events use the **child DocType** name:

```javascript
frappe.ui.form.on("Sales Invoice Item", {
    qty(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
    },
});
```

The checker accepts `frappe.ui.form.on` only for the requested DocType and its own child tables.

### frm API

| Call | Does |
|---|---|
| `frm.set_value("field", value)` | set a value (returns a promise) |
| `frm.doc.field` | read a value |
| `frm.set_df_property("field", "read_only" \| "hidden" \| "reqd" \| "label" \| "description" \| "options", value)` | change a property on this form only |
| `frm.toggle_display("field", show)`, `frm.toggle_reqd("field", req)`, `frm.toggle_enable("field", on)` | quick toggles |
| `frm.set_query("link_field", () => ({ filters: { disabled: 0 } }))` | filter a Link's choices (in `setup`); for a child field `frm.set_query("item_code", "items", (doc, cdt, cdn) => ({ filters: {...} }))` |
| `frm.add_custom_button(__("Label"), () => {...}, __("Group"))` | a button (in `refresh`) |
| `frm.refresh_field("field")`, `frm.refresh()` | redraw |
| `frm.is_new()`, `frm.doc.docstatus` | state |
| `frm.set_intro(__("text"), "blue")`, `frm.dashboard.set_headline(__("text"))` | messages on the form |
| `frappe.model.set_value(cdt, cdn, "field", value)` | set a value in a child row |
| `frappe.db.get_value("Customer", name, "customer_group").then(({ message }) => ...)` | read one value (async, with the user's permissions) |
| `frappe.xcall("method", {args}).then((r) => ...)` | call a whitelisted method or an API server script |
| `frappe.msgprint(__("text"))`, `frappe.show_alert({message: __("Saved"), indicator: "green"})`, `frappe.throw(__("text"))` | messages |
| `__("text")` | translate |

Everything that reads from the server is asynchronous: use `.then(...)` or `async`/`await`; never assume the value is
there on the next line.

## List scripts (`view: "List"`)

```javascript
frappe.listview_settings["Sales Invoice"] = {
    add_fields: ["outstanding_amount", "due_date"],        // extra fields to load per row
    filters: [["docstatus", "=", 1]],                       // default filters
    get_indicator(doc) {                                    // the status pill
        if (doc.outstanding_amount > 0 && doc.due_date < frappe.datetime.get_today()) return [__("Overdue"), "red", "due_date,<,Today"];
    },
    formatters: { customer(value) { return value.toUpperCase(); } },
    onload(listview) {
        listview.page.add_inner_button(__("Unpaid"), () => listview.filter_area.add([["Sales Invoice", "outstanding_amount", ">", 0]]));
    },
    button: { show: (doc) => doc.docstatus === 1, get_label: () => __("Print"), get_description: (doc) => doc.name, action: (doc) => frappe.set_route("print", "Sales Invoice", doc.name) },
};
```

The checker requires the script to assign `frappe.listview_settings["<doctype>"]`. Note that ERPNext already ships
`listview_settings` for many DocTypes; a client script replaces keys it sets.

## Refused by the checker

| Pattern | Level |
|---|---|
| Syntax error (parsed with acorn, latest ECMAScript) | critical, with line and column |
| `frappe.ui.form.on` for another DocType / no `listview_settings["<doctype>"]` for a List script | critical |
| `eval`, `new Function`, `setTimeout`/`setInterval` with a string, `import()` | critical |
| `document.cookie`, `localStorage`, `sessionStorage`, all of `frappe.boot`, `frappe.csrf_token` | critical |
| `fetch`, `XMLHttpRequest`, `$.ajax/get/post`, `navigator.sendBeacon`, `WebSocket`, `EventSource` to anything but `/api/…` | critical |
| `window.location` / `location.href =` to an absolute URL | critical |
| `"<script"`, `"<iframe"`, `.innerHTML =` with a non-constant value | high warning |
| `frappe.call`/`frappe.xcall` to `frappe.client.delete`/`set_value`/`submit`/`cancel` | warning; a method that does not exist: critical |
| more than 400 lines | warning |

Prefer `frm.set_df_property` / properties over DOM manipulation; prefer a property (`request_properties`) over a
script when it does the same thing (hide, require, read-only, label, depends_on).

## Example — require the customer's PO number for wholesale customers

```javascript
frappe.ui.form.on("Sales Invoice", {
    customer(frm) { set_po_required(frm); },
    refresh(frm) { set_po_required(frm); },
});

function set_po_required(frm) {
    if (!frm.doc.customer) return frm.toggle_reqd("po_no", false);
    frappe.db.get_value("Customer", frm.doc.customer, "customer_group").then(({ message }) => {
        frm.toggle_reqd("po_no", message && message.customer_group === "Wholesale");
    });
}
```

(A screen-only rule: if it must hold for imports too, add a Before Save server script.)
