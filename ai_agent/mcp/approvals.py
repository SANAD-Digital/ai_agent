"""Approval lifecycle for MCP hosts: prepare -> validate -> preview -> approval -> execute -> verify.

The card is rendered by the MCP App; buttons call the app-only tools ``approvals__decide`` and
``approvals__revise``. Each button carries a signed token that lives only in the tool result's
``_meta`` (never in content the model reads), so a decision needs the card the user was shown.
Hosts without MCP Apps fall back to text plus the Frappe desk link.
"""

import copy
import hashlib
import hmac
import json

import frappe
from frappe.utils import cint

from ai_agent.mcp import events
from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t
from ai_agent.presentation.labels import doctype_label, field_tier, label_for, status_display
from ai_agent.presentation.media import thumbnail_data_uri
from ai_agent.presentation.urls import doc_href
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import ApprovalAlreadyExecuted, ApprovalExpired
from ai_agent.runtime.exceptions import PermissionDenied
from ai_agent.runtime.permission_context import PermissionContext

# Labels below are English sources, translated with t() where they are shown.
STEPS = (("prepare", "Prepare"), ("validate", "Validate"), ("preview", "Review"),
         ("approval", "Approval"), ("execute", "Execute"), ("verify", "Confirm"))
ACTION_LABELS = {"Create": "Create", "Bulk Create": "Create several documents", "Update": "Edit", "Submit": "Submit",
                 "Cancel": "Cancel", "Workflow": "Workflow action", "Customize": "Customization"}
# Customization is approved in ERPNext only (design D4): never by a model-driven approvals__decide, never by a card button.
DESK_ONLY_TYPES = {"Customize"}
EDITABLE_TOP = {
    "company": ("Company", "link"), "posting_date": ("Date", "date"), "due_date": ("Due date", "date"),
    "transaction_date": ("Date", "date"), "delivery_date": ("Delivery date", "date"), "set_warehouse": ("Warehouse", "link"),
    "mode_of_payment": ("Mode of payment", "link"), "cost_center": ("Cost center", "link"), "remarks": ("Remarks", "text"),
    "project": ("Project", "link"), "branch": ("Branch", "link"),
}
EDITABLE_ITEM = {"qty": ("Quantity", "number"), "rate": ("Rate", "number"), "warehouse": ("Warehouse", "link"),
                 "discount_percentage": ("Discount %", "number")}
PREVIEW_PARTY = ("customer_name", "customer", "supplier_name", "supplier", "party", "party_name")


class ApprovalUiError(Exception):
    pass


def approval_required():
    """Does a write need a human decision at all? Off = requests run at once under the requester's own permissions."""
    value = frappe.get_cached_doc("AI Settings").get("mcp_require_approval")
    # Unset (field not migrated yet, or never saved) means REQUIRED: approval is only ever switched off on purpose.
    return True if value is None or value == "" else bool(cint(value))


def app_approval_enabled():
    """Are the card's Approve / Cancel buttons allowed? (Independent of whether approval is required, and of text approval.)"""
    settings = frappe.get_cached_doc("AI Settings")
    return bool(cint(settings.get("mcp_enable_action_requests")) and cint(settings.get("mcp_enable_app_approval", 0)) and approval_required())


def _secret():
    from frappe.utils.password import get_encryption_key

    return get_encryption_key().encode()


def _stored_result(approval):
    try:
        data = json.loads(approval.get("execution_result") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def four_eyes_enabled():
    return bool(cint(frappe.get_cached_doc("AI Settings").get("require_four_eyes")))


def issue_token(approval_name, payload_hash, user, action):
    """Bound to one approval, its exact payload, the user and one action (approve/reject/revise).
    Replay is closed by the approval status: a decided request is no longer Pending."""
    message = f"{approval_name}:{payload_hash}:{user}:{action}".encode()
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()[:40]


def _check_token(approval, user, token, action):
    expected = issue_token(approval.name, approval.payload_hash, user, action)
    if not token or not hmac.compare_digest(expected, str(token)):
        raise ApprovalUiError(t("This approval token is not valid. Ask the assistant to show the approval card again."))


def _visible(approval, user):
    if approval.requested_by == user:
        return True
    return ApprovalEngine(PermissionContext(user))._can_decide(approval, user)


def _load(approval_id, user):
    if not approval_id or not frappe.db.exists("AI Approval Request", approval_id):
        raise ApprovalUiError(t("Approval request not found."))
    approval = frappe.get_doc("AI Approval Request", approval_id)
    if not _visible(approval, user):
        raise ApprovalUiError(t("Approval request not found."))
    return approval


# ---------------------------------------------------------------- preview

DOC_ICONS = {"Customer": "user", "Supplier": "user", "Item": "package", "Company": "building", "Warehouse": "warehouse", "Sales Invoice": "receipt", "Purchase Invoice": "receipt", "Payment Entry": "wallet"}
TITLE_KEYS = ("customer_name", "supplier_name", "item_name", "item_code", "company_name", "warehouse_name", "name", "title")


def _bulk_documents(payload):
    """What a bulk approval will create, one readable block per document (never raw JSON)."""
    out = []
    for doc in (payload.get("documents") if isinstance(payload, dict) else None) or []:
        if not isinstance(doc, dict):
            continue
        doctype, body = doc.get("doctype"), doc.get("payload") or {}
        label = doctype_label(doctype)
        title = next((str(body[k]) for k in TITLE_KEYS if body.get(k)), label)
        fields = []
        for key, value in body.items():
            if key in ("doctype", "name") or isinstance(value, (list, dict)) or fmt.is_blank(value) or field_tier(key) == 4:
                continue
            fields.append({"label": label_for(key, doctype), "d": fmt.date(value) if "date" in key else fmt.text(value)})
            if len(fields) >= 6:
                break
        out.append({"doctype": doctype, "type_label": label, "title": title, "fields": fields, "icon": DOC_ICONS.get(doctype, "file")})
    return out


PARTY_DOCTYPES = {"Sales Invoice": ("customer", "Customer"), "Sales Order": ("customer", "Customer"), "Quotation": ("party_name", "Customer"), "Delivery Note": ("customer", "Customer"),
                  "Purchase Invoice": ("supplier", "Supplier"), "Purchase Order": ("supplier", "Supplier"), "Purchase Receipt": ("supplier", "Supplier")}
# How a sentence names the document ("approve and create the invoice"); English sources, translated where used.
DEFINITE = {"Sales Invoice": "the invoice", "Purchase Invoice": "the invoice", "Delivery Note": "the delivery note", "Sales Order": "the sales order", "Purchase Order": "the purchase order"}


def _definite(doctype, type_label):
    return t(DEFINITE[doctype]) if doctype in DEFINITE else type_label


def _preview(action_type, doctype, payload):
    """Validate and preview without saving anything."""
    warnings, fields, items, kpis = [], [], [], []
    empty = {"fields": fields, "items": items, "kpis": kpis, "warnings": warnings, "valid": True, "photo": None, "blocked": None,
             "impact": _impact(action_type, doctype, {}, [], None, None), "details": []}
    if action_type != "Create" or not doctype or not isinstance(payload, dict):
        return empty
    doc, valid = None, True
    # Validate hooks can write (other apps' controllers): whatever they do is rolled back.
    frappe.db.savepoint("ai_card_preview")
    try:
        doc = frappe.get_doc({**payload, "doctype": doctype})
        if doc.meta.get_field("items"):
            for method in ("set_missing_values", "calculate_taxes_and_totals"):
                if hasattr(doc, method):
                    getattr(doc, method)()
        doc.run_method("validate")
    except Exception as exc:
        valid = False
        warnings.append(schema.warning("critical", _plain_error(exc), "validation"))
    finally:
        frappe.db.rollback(save_point="ai_card_preview")
    source = doc.as_dict() if doc else payload
    currency = source.get("currency") or source.get("default_currency")
    for idx, row in enumerate(source.get("items") or [], start=1):
        item = row if isinstance(row, dict) else {}
        qty, rate = fmt.to_number(item.get("qty")), fmt.to_number(item.get("rate"))
        amount = item.get("amount") if item.get("amount") is not None else (qty or 0) * (rate or 0)
        code = item.get("item_code")
        info = _item_info(code, item, source)
        items.append({"idx": idx, "item": item.get("item_name") or info.get("item_name") or code, "item_code": code, "qty": item.get("qty"), "rate": item.get("rate"),
                      "amount": amount, "warehouse": info["warehouse"], "uom": info["uom"], "image": info["image"], "stock": info["stock"],
                      "d": {"qty": fmt.number(qty), "rate": fmt.money(rate, currency), "amount": fmt.money(amount, currency)}})
    total = source.get("grand_total")
    if total in (None, "") and items:
        total = sum(fmt.to_number(i["amount"]) or 0 for i in items)
    if total not in (None, ""):
        kpis.append(schema.kpi("grand_total", t("Total"), total, currency=currency, tone="info"))
    fields.extend(_facts(doctype, source, items, currency, total))
    stock_warnings, blocked = _stock_check(doctype, items, source)
    warnings += stock_warnings
    photo = next((thumbnail_data_uri(i["image"]) for i in items if i["image"]), None)
    return {"fields": fields, "items": items, "kpis": kpis, "warnings": warnings, "valid": valid, "currency": currency, "photo": photo,
            "blocked": _blocked(doctype, blocked, source) if blocked else None,
            "impact": _impact(action_type, doctype, source, items, currency, total), "details": _details(doctype, payload)}


def _details(doctype, payload):
    """Everything the request will write, as labelled values (the "details" section): what was asked for, nothing computed."""
    from ai_agent.presentation.presenters.documents import build_document

    try:
        presentation, _, _ = build_document(doctype, "—", {**payload, "doctype": doctype, "name": "—"})
    except Exception:
        return []
    out = []
    for section in presentation["sections"]:
        out.append({"title": section["title"], "fields": [{"label": f["label"], "d": f["d"]} for f in section["fields"]]})
    for table in presentation["tables"]:
        cols = [c for c in table["columns"] if not c.get("collapsed")]
        out.append({"title": table["title"], "columns": [c["label"] for c in cols],
                    "rows": [[r["cells"].get(c["key"], {}).get("d", "—") for c in cols] for r in table["rows"][:20]]})
    return out


def _item_info(code, row, source):
    """Warehouse, unit, picture and availability of one line, from data the caller can read."""
    info = {"warehouse": row.get("warehouse") or source.get("set_warehouse"), "uom": row.get("uom") or row.get("stock_uom"), "image": None, "stock": None, "item_name": None}
    if not code:
        return info
    try:
        item = frappe.db.get_value("Item", code, ["item_name", "image", "stock_uom", "is_stock_item"], as_dict=True)
        if not item:
            return info
        info.update({"item_name": item.item_name, "image": item.image, "uom": info["uom"] or item.stock_uom})
        if not info["warehouse"] and source.get("company"):
            info["warehouse"] = frappe.db.get_value("Item Default", {"parent": code, "company": source.get("company")}, "default_warehouse")
        if item.is_stock_item and info["warehouse"]:
            from erpnext.stock.utils import get_stock_balance

            available = get_stock_balance(code, info["warehouse"]) or 0
            qty = fmt.to_number(row.get("qty")) or 0
            info["stock"] = {"available": available, "d": fmt.number(available), "ok": available >= qty}
    except Exception:
        pass
    return info


def _facts(doctype, source, items, currency, total):
    """The facts grid of the ready card: who, what, how many, how much, from which company."""
    party_key, party_label = PARTY_DOCTYPES.get(doctype, (None, None))
    if not party_key:
        out = []
        for key in PREVIEW_PARTY + ("company", "posting_date", "transaction_date", "due_date", "mode_of_payment", "set_warehouse", "cost_center", "branch", "project", "remarks"):
            value = source.get(key)
            if fmt.is_blank(value) or any(f["key"] == key for f in out) or (key in ("customer", "supplier") and source.get(f"{key}_name")):
                continue
            out.append({"key": key, "label": label_for(key, doctype), "d": fmt.date(value) if "date" in key else str(value), "tier": field_tier(key)})
        return sorted(out, key=lambda f: f["tier"])
    party = source.get(f"{party_key}_name") or source.get("customer_name") or source.get("supplier_name") or source.get(party_key)
    facts = [{"key": "party", "label": t(party_label), "d": str(party or "—"), "icon": "user", "link": True}]
    if len(items) == 1:
        it = items[0]
        unit = f" {it['uom']}" if it.get("uom") else ""
        chip = None
        if it["stock"] is not None:
            if it["stock"]["ok"]:
                chip = {"d": t("In stock"), "tone": "success"}
            elif deducts_stock(doctype, source):
                chip = {"d": t("Less available than requested"), "tone": "danger"}
            else:       # the document does not move stock: availability is a note, not a problem
                chip = {"d": t("Available: {0}", fmt.number(it['stock']['available'])), "tone": "neutral"}
        facts += [{"key": "qty", "label": t("Quantity"), "d": f"{it['d']['qty']}{unit}", "icon": "hash"},
                  {"key": "item", "label": t("Item"), "d": str(it["item"] or "—"), "icon": "package", "link": True, "chip": chip},
                  {"key": "rate", "label": t("Price per {0}", it["uom"]) if it["uom"] else t("Unit price"), "d": it["d"]["rate"], "icon": "coins"}]
    payment = source.get("mode_of_payment") or next((p.get("mode_of_payment") for p in (source.get("payments") or []) if isinstance(p, dict) and p.get("mode_of_payment")), None)
    if payment:
        facts.append({"key": "payment", "label": t("Mode of payment"), "d": str(payment), "icon": "card"})
    if total not in (None, ""):
        facts.append({"key": "total", "label": t("Total"), "d": fmt.money(total, currency), "icon": "receipt"})
    if source.get("company"):
        facts.append({"key": "company", "label": t("Company"), "d": str(source.get("company")), "icon": "building", "link": True})
    if source.get("due_date") and doctype in ("Sales Invoice", "Purchase Invoice"):
        facts.append({"key": "due", "label": t("Due date"), "d": fmt.date(source.get("due_date")), "icon": "calendar"})
    return facts


def deducts_stock(doctype, source):
    """Will executing and later submitting this document move stock?"""
    if doctype in ("Delivery Note", "Stock Entry", "Purchase Receipt"):
        return True
    return doctype in ("Sales Invoice", "Purchase Invoice") and bool(cint((source or {}).get("update_stock")))


def _stock_check(doctype, items, source):
    """Warnings, plus the first blocking shortage. A shortage blocks only when the document deducts stock;
    an invoice with update_stock = 0 never touches stock, so there it is information and nothing more."""
    outgoing = doctype in ("Delivery Note", "Sales Invoice")
    deducts = outgoing and deducts_stock(doctype, source)
    warnings, blocked = [], None
    for it in items:
        stock = it.get("stock")
        if not stock or stock["ok"] or not outgoing:
            continue
        unit = f" {it['uom']}" if it.get("uom") else ""
        if deducts:
            warnings.append(schema.warning("critical", t("Not enough stock of {0}: {1} available, {2} requested.", it["item"], f"{fmt.number(stock['available'])}{unit}", f"{fmt.number(it['qty'])}{unit}"), "insufficient_stock"))
            blocked = blocked or it
        else:
            warnings.append(schema.warning("info", t("Note: {1} of {0} is in stock and {2} was requested. This invoice does not deduct stock, so it can still be created.",
                                                     it["item"], f"{fmt.number(stock['available'])}{unit}", f"{fmt.number(it['qty'])}{unit}"), "stock_below_requested"))
    return warnings, blocked


def _impact(action_type, doctype, source, items, currency, total):
    """What approving will change: money, stock, and what happens next. Facts only, from the prepared document."""
    from ai_agent.presentation import money

    financial, stock, steps = [], [], []
    if total not in (None, ""):
        financial.append({"label": t("Document total"), "d": fmt.money(total, currency)})
        taxes = fmt.to_number(source.get("total_taxes_and_charges"))
        if taxes:
            financial.append({"label": t("Of which taxes and charges"), "d": fmt.money(taxes, currency)})
        if money.is_multi_currency(source) and fmt.to_number(source.get("base_grand_total")):
            financial.append({"label": t("In company currency"), "d": fmt.money(source.get("base_grand_total"), money.company_currency(source.get("company"))),
                              "hint": money.exchange_rate_text(source)})
    if doctype in ("Sales Invoice", "Purchase Invoice", "Delivery Note", "Purchase Receipt", "Stock Entry") and items:
        if deducts_stock(doctype, source):
            outgoing = doctype in ("Sales Invoice", "Delivery Note")
            for it in items[:5]:
                unit = f" {it['uom']}" if it.get("uom") else ""
                warehouse = it.get("warehouse") or t("the default warehouse")
                moved = f"{fmt.number(it['qty'])}{unit}"
                stock.append({"label": str(it["item"]), "d": t("Taken from {0}: {1}", warehouse, moved) if outgoing else t("Added to {0}: {1}", warehouse, moved)})
        else:
            stock.append({"label": t("Stock"), "d": t("Not affected: stock update is off for this document")})
    if action_type in ("Create", "Bulk Create"):
        steps = [t("The document is created as a draft and gets its number from ERPNext."), t("Nothing is posted to the accounts and no balance changes until the draft is submitted."), t("We check that the document exists and show its link.")]
    elif action_type == "Submit":
        steps = [t("The document is submitted and posted to the general ledger."), t("Once submitted it cannot be edited, only cancelled."), t("We check its status afterwards.")]
    elif action_type == "Cancel":
        steps = [t("The document is cancelled and its entries are reversed."), t("We check its status afterwards.")]
    elif action_type == "Update":
        steps = [t("The changes are saved, provided the document has not changed since the request was prepared."), t("We check the document afterwards.")]
    elif action_type == "Workflow":
        steps = [t("The workflow action runs with your permissions."), t("We check the new status.")]
    return {"financial": financial, "stock": stock, "next": steps}


def _blocked(doctype, it, source):
    """The insufficient-stock card: what is short, and where the item is available instead."""
    alternatives = []
    try:
        if frappe.has_permission("Bin", "read"):
            need = fmt.to_number(it["qty"]) or 0
            for row in frappe.get_list("Bin", filters={"item_code": it["item_code"], "actual_qty": [">=", need], "warehouse": ["!=", it["warehouse"] or ""]},
                                       fields=["warehouse", "actual_qty"], order_by="actual_qty desc", limit_page_length=5):
                alternatives.append({"warehouse": row.warehouse, "available": row.actual_qty, "d": f"{fmt.number(row.actual_qty)} {it.get('uom') or ''}".strip()})
    except Exception:
        alternatives = []
    label = _definite(doctype, doctype_label(doctype))
    return {"title": t("Cannot approve {0}", label),
            "message": t("The requested quantity is not available in the selected warehouse."), "item": it["item"], "item_code": it["item_code"], "warehouse": it["warehouse"],
            "image": thumbnail_data_uri(it.get("image")), "requested": fmt.to_number(it["qty"]), "available": it["stock"]["available"], "unit": it.get("uom"),
            "alternatives": alternatives}


def _plain_error(exc):
    text = frappe.utils.strip_html(str(exc)).strip()
    return text[:300] or t("The document failed validation.")


# ---------------------------------------------------------------- card

def _steps(state, valid=True):
    """Lifecycle strip: how far the request got and where it stopped."""
    # (index of the first step not yet done, is the stopping step an error)
    position = {"pending": (3, False), "approved": (4, False), "executed": (5, False), "verified": (6, False),
                "failed": (4, True), "rejected": (3, True), "expired": (3, True)}[state]
    current, failed = position
    if not valid:
        current, failed = 1, True
    out = []
    for index, (key, label) in enumerate(STEPS):
        if index < current:
            step_state = "done"
        elif index == current:
            step_state = "error" if failed else "active"
        else:
            step_state = "todo"
        out.append({"key": key, "label": t(label), "state": step_state})
    return out


def build_card(approval, user, *, state=None, result=None, error=None):
    if approval.action_type in DESK_ONLY_TYPES:
        return _customize_card(approval, user, state=state, error=error)
    payload = json.loads(approval.payload_json or "{}")
    stored = _stored_result(approval)
    if not state:
        # Reading a request back (status check, repeated approval): the recorded outcome decides.
        state = {"Pending": "pending", "Approved": "approved", "Executed": "executed", "Rejected": "rejected",
                 "Expired": "expired", "Failed": "failed"}.get(approval.status, "pending")
        if approval.status == "Executed":
            state = "verified" if stored.get("verified") else "executed"
    if state in ("executed", "verified"):
        result = result or stored
    if approval.status == "Failed" and not error:
        error = stored.get("error")
    doctype = approval.doctype_name
    type_label = doctype_label(doctype) if doctype else ""
    action_label = t(ACTION_LABELS[approval.action_type]) if approval.action_type in ACTION_LABELS else approval.action_type
    preview = _preview(approval.action_type, doctype, payload) if state == "pending" else {
        "fields": [], "items": [], "kpis": [], "warnings": [], "valid": True, "photo": None, "blocked": None, "impact": {"financial": [], "stock": [], "next": []}, "details": []}
    preview.setdefault("impact", _impact(approval.action_type, doctype, {}, [], None, None))
    preview.setdefault("details", [])
    if approval.action_type == "Bulk Create":
        preview["fields"].append({"key": "count", "label": t("Number of documents"), "d": fmt.number(len(payload.get("documents") or payload.get("items") or []))})
    if approval.action_type in ("Update", "Submit", "Cancel", "Workflow") and approval.document_name:
        preview["fields"].append({"key": "document", "label": t("Document"), "d": f"{type_label} {approval.document_name}"})
        if approval.action_type == "Update":
            for key, value in payload.items() if isinstance(payload, dict) else []:
                if key not in ("doctype", "name") and not isinstance(value, (list, dict)):
                    preview["fields"].append({"key": key, "label": label_for(key, doctype), "d": fmt.text(value)})
    if error:
        preview["warnings"].append(schema.warning("critical", error, "execution"))
    desk = doc_href("AI Approval Request", approval.name)
    can_act = state == "pending" and preview["valid"] and app_approval_enabled() and frappe.flags.get("mcp_ui_supported") is True
    blocked = preview.get("blocked")
    # Four-eyes policy: the engine refuses to let a requester approve their own request, so do not offer a button that can only fail.
    own_request_blocked = bool(four_eyes_enabled() and approval.requested_by == user)
    if own_request_blocked and state == "pending":
        preview["warnings"].append(schema.warning("info", t("The four-eyes policy is on: you cannot approve your own request. An authorised ERP user approves it; you can still edit or cancel it here."), "four_eyes"))

    def args(action):
        return {"approval_id": approval.name, "token": issue_token(approval.name, approval.payload_hash, user, action)}

    creating = approval.action_type == "Create" and isinstance(payload, dict)
    bulk = approval.action_type == "Bulk Create" and isinstance(payload, dict)
    documents = _bulk_documents(payload) if bulk and state == "pending" else None
    actions = []
    if can_act:
        approve_label = t("Approve and create {0}", _definite(doctype, type_label)) if creating else (t("Approve and create the documents") if bulk else t("Approve and run"))
        cancel = schema.action("cancel", t("Cancel"), kind="tool", style="danger", tool="approvals__decide", arguments={**args("reject"), "decision": "reject"}, confirm=True, icon="x")
        edit = schema.action("edit", t("Edit quantity") if blocked else t("Edit"), kind="edit", style="soft", arguments=args("revise"), icon="edit") if creating else None
        if blocked:
            actions = [a for a in (
                schema.action("view_stock", t("View stock"), kind="drill", style="primary", tool="erp__stock_balance",
                              arguments={"item_code": blocked["item_code"], "warehouse": blocked["warehouse"]}, icon="box") if blocked.get("warehouse") else None,
                schema.action("choose_warehouse", t("Choose another warehouse"), kind="choose", style="secondary", icon="refresh") if creating else None,
                edit, cancel) if a]
        else:
            approve = None if own_request_blocked else schema.action("approve", approve_label, kind="tool", style="success", tool="approvals__decide",
                                                                     arguments={**args("approve"), "decision": "approve"}, icon="check")
            actions = [a for a in (approve, edit, cancel) if a]
    doc_name = (result or {}).get("document_name")
    result_doctype = (result or {}).get("doctype") or doctype
    if state in ("executed", "verified") and doc_name:
        actions = [schema.action("open", t("Open document"), href=doc_href(result_doctype, doc_name), style="primary")]
    warnings = preview["warnings"]
    if any(w["level"] == "critical" for w in warnings) and can_act and not blocked:
        actions = [a for a in actions if a["key"] != "approve"]
    if creating and state == "pending":
        title, subtitle, status_label = (t("{0} — ready to create", type_label),
                                         t("We prepared {0} from your request. Please review the details and approve.", _definite(doctype, type_label)), t("Needs approval"))
    elif documents:
        n = len(documents)
        title = t("{0} documents ready to create", fmt.number(n)) if n > 2 else t("Two documents ready to create")
        subtitle, status_label = approval.action_summary or t("Please review the documents below and approve them all at once."), t("Needs approval")
    else:
        title, subtitle, status_label = f"{action_label} {type_label}".strip(), t("Request {0}", approval.name), None
        if stored.get("mode") == "direct":
            subtitle = t("Run directly: approval is not required in the current settings")
    return {
        "type": "approval", "state": state, "approval_id": approval.name,
        "title": title, "subtitle": subtitle, "status_label": status_label,
        "fields": preview["fields"], "items": preview["items"], "kpis": preview["kpis"], "photo": preview.get("photo"), "documents": documents,
        "warnings": warnings, "impact": None if (creating or documents) else (approval.impact_summary or None),
        "blocked": blocked if state == "pending" else None,
        "operation": {"action": action_label, "doctype": type_label, "document": approval.document_name, "requested_by": approval.requested_by},
        "impact_financial": preview["impact"]["financial"], "impact_stock": preview["impact"]["stock"],
        "next_steps": _next_steps(approval, preview["impact"]["next"]) if state == "pending" else [],
        "details": preview["details"],
        "expires_at": str(approval.expires_at) if approval.expires_at else None,
        "steps": _steps(state, preview["valid"]),
        "actions": actions, "insights": [],
        "editable": _editable(payload, doctype) if can_act and creating else None,
        "result": {"document_name": doc_name, "doctype": result_doctype, "href": doc_href(result_doctype, doc_name),
                   "documents": (result or {}).get("documents")} if result else None,
        "fallback": {"desk_url": desk, "text": t("To approve, reply \"approve\" in the chat, or approve request {0} in ERPNext: {1}", approval.name, desk)},
        "currency": preview.get("currency"),
        "developer": {"approval_id": approval.name, "action_type": approval.action_type, "doctype": doctype,
                      "payload_hash": approval.payload_hash, "run": approval.run, "requested_by": approval.requested_by},
    }


def _customize_card(approval, user, *, state=None, error=None):
    """A customization request: diffs, the full code, the analyzers' findings, the impact and the undo plan
    (customization.presenter). Approval happens in ERPNext only, so the card carries no approve button; it links
    to the request in the desk. After execution it offers the created record and an undo follow-up."""
    from ai_agent.customization import presenter

    stored = _stored_result(approval)
    section = presenter.section(approval, user) or {}
    if state and state != section.get("state") and state in ("executed", "verified", "failed", "rejected", "expired", "approved"):
        section["state"] = state
    state = section.get("state") or "pending"
    desk = doc_href("AI Approval Request", approval.name)
    warnings = []
    if section.get("tainted"):
        warnings.append(schema.warning("warning", section["tainted_text"], "tainted"))
    if error or (state == "failed" and stored.get("error")):
        warnings.append(schema.warning("critical", error or stored.get("error"), "execution"))
    if state == "pending":
        warnings.append(schema.warning("info", t("Customization requests are approved in ERPNext only. Open the request there to review and approve it."), "desk_only"))
    actions = []
    if state == "pending":
        label = t("Open the request in ERPNext") if section.get("has_critical") else t("Review and approve in ERPNext")
        actions.append(schema.action("open_request", label, href=desk, style="primary", icon="external"))
        if app_approval_enabled() and frappe.flags.get("mcp_ui_supported") is True:
            actions.append(schema.action("cancel", t("Cancel"), kind="tool", style="danger", tool="approvals__decide",
                                         arguments={"approval_id": approval.name, "token": issue_token(approval.name, approval.payload_hash, user, "reject"), "decision": "reject"},
                                         confirm=True, icon="x"))
    result = section.get("result") or {}
    for target in (result.get("targets") or [])[:3]:
        actions.append(schema.action("open", target["label"], href=target["href"], style="primary" if not actions else "secondary", icon="external"))
    if result.get("undo_prompt"):
        actions.append(schema.action("undo", result["undo_label"], kind="message", style="soft", arguments={"text": result["undo_prompt"]}, icon="refresh"))
    title = " — ".join(x for x in (section.get("kind_label"), section.get("target")) if x) or t("Customization")
    first = (result.get("targets") or [{}])[0] if result else {}
    return {
        "type": "approval", "state": state, "approval_id": approval.name,
        "title": title, "subtitle": section.get("purpose") or approval.action_summary or t("Request {0}", approval.name),
        "status_label": t("Needs approval") if state == "pending" else None,
        "fields": [], "items": [], "kpis": [], "photo": None, "documents": None,
        "warnings": warnings, "impact": None, "blocked": None,
        "operation": {"action": t("Customization"), "doctype": section.get("kind_label"), "document": section.get("name"), "requested_by": approval.requested_by},
        "impact_financial": [], "impact_stock": [],
        "next_steps": section.get("next_steps") or [],
        "details": [], "expires_at": str(approval.expires_at) if approval.expires_at else None,
        "steps": _steps(state, not section.get("has_critical")),
        "actions": actions, "insights": [], "editable": None,
        "result": {"document_name": first.get("name"), "doctype": first.get("doctype"), "href": first.get("href"),
                   "documents": [{"doctype": x["doctype"], "document_name": x["name"]} for x in result.get("targets") or []]} if result and state != "failed" else None,
        "fallback": {"desk_url": desk, "text": t("Customization requests are approved in ERPNext only: open request {0}: {1}", approval.name, desk)},
        "currency": None,
        "customization": section,
        "developer": {"approval_id": approval.name, "action_type": approval.action_type, "kind": section.get("kind"),
                      "payload_hash": approval.payload_hash, "run": approval.run, "requested_by": approval.requested_by},
    }


def _next_steps(approval, steps):
    from ai_agent.mcp import continuation

    steps = list(steps)
    then = continuation.peek(approval.name) if approval.name else None
    if then:
        steps.append(t("Then we continue automatically with your original goal: {0}.", then["goal"]))
    return steps


def _editable(payload, doctype):
    fields = []
    for key, (label, kind) in EDITABLE_TOP.items():
        if key in payload:
            fields.append({"key": key, "label": t(label), "type": kind, "value": payload.get(key)})
    items = []
    for idx, row in enumerate(payload.get("items") or [], start=1):
        if isinstance(row, dict):
            items.append({"idx": idx, "label": row.get("item_name") or row.get("item_code"),
                          "fields": [{"key": k, "label": t(lab), "type": kind, "value": row.get(k)} for k, (lab, kind) in EDITABLE_ITEM.items() if k in row]})
    return {"fields": fields, "items": items} if (fields or items) else None


CARD_STATES = {"pending": "Waiting for your approval", "approved": "Approval recorded", "executed": "Done", "verified": "Done and verified",
               "rejected": "Request rejected", "expired": "Request expired", "failed": "Execution failed"}


def card_envelope(approval, user, *, tool="approvals", state=None, result=None, error=None):
    card = build_card(approval, user, state=state, result=result, error=error)
    state_text = t(CARD_STATES[card["state"]])
    text = f"{card['title']} — {approval.name}: {state_text}"
    for k in card["kpis"][-1:]:
        text = t("{0}, {1}", text, f"{k['label']} {k['d']}")
    if card["state"] == "pending" and approval.action_type in DESK_ONLY_TYPES:
        text += ". " + card["fallback"]["text"]
    elif card["state"] == "pending":
        # Text hosts (and the model) need to know the request can be decided right here in the chat.
        text += t(". To approve reply \"approve\", to cancel reply \"cancel\" (request {0})", approval.name)
    return schema.envelope(
        tool=tool, data={"approval_id": approval.name, "status": approval.status, "action_type": approval.action_type,
                         "doctype": approval.doctype_name, "result": card["result"]},
        summary={"text": text, "approval_id": approval.name, "state": card["state"]},
        presentation=card, warnings=card["warnings"], meta={"approval_id": approval.name})


# ---------------------------------------------------------------- app-only operations

def status(user, approval_id):
    approval = _load(approval_id, user)
    events.log("approval_loaded", user=user, approval_id=approval.name, doctype=approval.doctype_name, status=approval.status)
    return card_envelope(approval, user, tool="approvals.status")


def list_pending(user):
    """The user's own pending requests: what "موافق" can refer to."""
    rows = frappe.get_all("AI Approval Request", filters={"status": "Pending", "requested_by": user, "expires_at": [">", frappe.utils.now_datetime()]},
                          fields=["name", "action_type", "doctype_name", "action_summary", "creation"], order_by="creation desc", limit=10)
    if not rows:
        return schema.envelope(tool="approvals.list_pending", data={"pending": []}, summary={"text": t("No pending approval requests."), "count": 0})
    if len(rows) == 1:
        approval = frappe.get_doc("AI Approval Request", rows[0].name)
        envelope = card_envelope(approval, user, tool="approvals.list_pending")
        envelope["data"]["pending"] = [rows[0].name]
        return envelope
    from ai_agent.presentation.presenters import choice

    options = [{"value": r.name, "label": (r.action_summary or f"{t(ACTION_LABELS[r.action_type]) if r.action_type in ACTION_LABELS else r.action_type} {doctype_label(r.doctype_name) if r.doctype_name else ''}").strip()[:140],
                "meta": r.name, "icon": "file"} for r in rows]
    envelope = choice.build(t("Which request do you mean?"), options, subtitle=t("You have more than one pending approval request. Choose the one you want to decide on."), icon="file",
                            reply_template=t("I mean request {value}"))
    envelope["data"]["pending"] = [r.name for r in rows]
    return envelope


def decide(user, approval_id, decision, token=None, reason=None):
    """The one place an MCP decision is taken: approve needs the card's signed token; reject may come from chat.

    Exactly-once: a second "approve" returns the outcome of the first, it never runs the action again.
    """
    via = "app" if token else "chat"
    events.log("approval_decision_received", user=user, approval_id=approval_id, decision=decision, via=via)
    if decision not in ("approve", "reject"):
        raise ApprovalUiError(t("Unknown decision: use approve or reject."))
    if not approval_required():
        raise ApprovalUiError(t("Approval is not required in the current settings; requests run directly."))
    approval = _load(approval_id, user)
    events.log("approval_loaded", user=user, approval_id=approval.name, doctype=approval.doctype_name, status=approval.status)
    if decision == "approve" and approval.action_type in DESK_ONLY_TYPES and approval.status != "Executed":
        # D4: an outside model must never approve code or schema changes; a person approves them in ERPNext.
        raise ApprovalUiError(t("Customization requests are approved in ERPNext only: open request {0}: {1}", approval.name, doc_href("AI Approval Request", approval.name)))
    if decision == "approve" and not token and approval.status not in ("Executed", "Rejected"):
        # The external model cannot approve on the user's behalf (a word in the chat, or text inside a record the
        # model read, could trigger it): approval is a click on the card's signed button, or in ERPNext.
        raise ApprovalUiError(t("Approve by clicking Approve on the approval card, or in ERPNext: {0}", doc_href("AI Approval Request", approval.name)))
    if token:
        if not app_approval_enabled():
            raise ApprovalUiError(t("Approving from the card is turned off. Approve the request in ERPNext: {0}", doc_href("AI Approval Request", approval.name)))
        _check_token(approval, user, token, decision)

    # A repeated decision is answered with the recorded outcome, never executed again.
    if approval.status == "Executed" and decision == "approve":
        return card_envelope(approval, user, tool="approvals.decide")
    if approval.status == "Rejected" and decision == "reject":
        return card_envelope(approval, user, tool="approvals.decide", state="rejected")
    if approval.status not in ("Pending", "Approved"):
        raise ApprovalUiError(t("This request can no longer be decided: its status is {0}.", status_display(approval.status)[0]))

    engine = ApprovalEngine(PermissionContext(user))
    try:
        if decision == "reject":
            approval = engine.reject(approval.name, reason or f"Rejected from {via}", user)
            events.log("approval_rejected", user=user, approval_id=approval.name, doctype=approval.doctype_name, via=via)
            return card_envelope(approval, user, tool="approvals.decide", state="rejected")
        if approval.status == "Pending":
            try:
                approval = engine.approve(approval.name, user)
            except ApprovalAlreadyExecuted:
                return _outcome(user, approval.name)     # a concurrent decision got there first
            except ApprovalExpired:
                raise ApprovalUiError(t("This approval request has expired. Ask the assistant to prepare it again."))
            events.log("approval_approved", user=user, approval_id=approval.name, doctype=approval.doctype_name, via=via)
        events.log("approval_execute_started", user=user, approval_id=approval.name, doctype=approval.doctype_name)
        try:
            result = engine.execute_approved_action(approval.name)
        except ApprovalAlreadyExecuted:
            return _outcome(user, approval.name)
        except ApprovalExpired as exc:
            raise ApprovalUiError(str(exc))
        except Exception as exc:
            events.log("approval_failed", user=user, approval_id=approval.name, doctype=approval.doctype_name, error=_plain_error(exc)[:200])
            approval.reload()
            return card_envelope(approval, user, tool="approvals.decide", state="failed", error=_plain_error(exc))
        events.log("approval_execute_completed", user=user, approval_id=approval.name, doctype=approval.doctype_name)
        approval.reload()
        verified = record_verification(user, approval, result, via=via)
        approval.reload()
        return card_envelope(approval, user, tool="approvals.decide", state="verified" if verified else "executed", result=result)
    except PermissionDenied as exc:
        raise ApprovalUiError(str(exc))


def _outcome(user, approval_name):
    approval = frappe.get_doc("AI Approval Request", approval_name)
    return card_envelope(approval, user, tool="approvals.decide")


def record_verification(user, approval, result, *, via=None, mode=None):
    """Re-read what the action claims it changed and keep the verdict with the execution result."""
    events.log("approval_verify_started", user=user, approval_id=approval.name, doctype=approval.doctype_name)
    ok = _verify(result, approval.action_type)
    stored = _stored_result(approval) or (result if isinstance(result, dict) else {})
    stored = {**stored, "verified": ok, "verified_at": str(frappe.utils.now_datetime()), "approved_via": via}
    if mode:
        stored["mode"] = mode
    frappe.db.set_value("AI Approval Request", approval.name, "execution_result", json.dumps(stored, ensure_ascii=False, default=str), update_modified=False)
    frappe.db.commit()
    events.log("approval_verified" if ok else "approval_failed", user=user, approval_id=approval.name, doctype=approval.doctype_name, verified=ok)
    return ok


def _verify(result, action_type=None):
    """Re-read what the approval claims it created. A customization verifies itself (kind.verify re-reads the meta,
    the script or the report) and records the verdict in execution_result.verified."""
    if not isinstance(result, dict):
        return False
    if action_type in DESK_ONLY_TYPES:
        return result.get("verified") is True
    if result.get("document_name") and result.get("doctype"):
        return bool(frappe.db.exists(result["doctype"], result["document_name"]))
    docs = result.get("documents") or []
    return bool(docs) and all(frappe.db.exists(d.get("doctype") or result.get("doctype"), d.get("document_name")) for d in docs if isinstance(d, dict))


def revise(user, approval_id, token, changes):
    """Edit a pending Create before approval: same checks as a fresh request, old request withdrawn."""
    approval = _load(approval_id, user)
    _check_token(approval, user, token, "revise")
    if approval.status != "Pending" or approval.action_type != "Create":
        raise ApprovalUiError(t("This request cannot be edited."))
    payload = copy.deepcopy(json.loads(approval.payload_json))
    for key, value in ((changes or {}).get("fields") or {}).items():
        allowed_new = key == "set_warehouse" and frappe.get_meta(approval.doctype_name).has_field("set_warehouse")
        if key not in EDITABLE_TOP or (key not in payload and not allowed_new):
            raise ApprovalUiError(t("This field cannot be edited: {0}", key))
        payload[key] = value
        if key == "set_warehouse":
            for row in payload.get("items") or []:
                if isinstance(row, dict):
                    row["warehouse"] = value
    for idx, edits in ((changes or {}).get("items") or {}).items():
        try:
            row = payload["items"][int(idx) - 1]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ApprovalUiError(t("Row not found."))
        for key, value in (edits or {}).items():
            if key not in EDITABLE_ITEM or key not in row:
                raise ApprovalUiError(t("This field cannot be edited: {0}", key))
            row[key] = fmt.to_number(value) if EDITABLE_ITEM[key][1] == "number" else value
    engine = ApprovalEngine(PermissionContext(user))
    try:
        new = engine.create_approval_request(
            run_name=approval.run, action_type=approval.action_type, doctype_name=approval.doctype_name,
            document_name=approval.document_name, action_summary=approval.action_summary,
            impact_summary=approval.impact_summary or "", payload=payload)
    except PermissionDenied as exc:
        raise ApprovalUiError(str(exc))
    engine.reject(approval.name, "Revised before approval", user)
    return card_envelope(new, user, tool="approvals.revise")
