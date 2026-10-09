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
from ai_agent.presentation.labels import doctype_label, field_tier, label_for
from ai_agent.presentation.media import thumbnail_data_uri
from ai_agent.presentation.urls import doc_href
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import ApprovalAlreadyExecuted, ApprovalExpired
from ai_agent.runtime.exceptions import PermissionDenied
from ai_agent.runtime.permission_context import PermissionContext

STEPS = (("prepare", "تجهيز"), ("validate", "تحقق"), ("preview", "مراجعة"),
         ("approval", "موافقة"), ("execute", "تنفيذ"), ("verify", "تأكيد"))
ACTION_LABELS = {"Create": "إنشاء", "Bulk Create": "إنشاء عدة مستندات", "Update": "تعديل", "Submit": "اعتماد",
                 "Cancel": "إلغاء", "Workflow": "إجراء سير عمل"}
EDITABLE_TOP = {
    "company": ("الشركة", "link"), "posting_date": ("التاريخ", "date"), "due_date": ("الاستحقاق", "date"),
    "transaction_date": ("التاريخ", "date"), "delivery_date": ("التسليم", "date"), "set_warehouse": ("المخزن", "link"),
    "mode_of_payment": ("طريقة الدفع", "link"), "cost_center": ("مركز التكلفة", "link"), "remarks": ("البيان", "text"),
    "project": ("المشروع", "link"), "branch": ("الفرع", "link"),
}
EDITABLE_ITEM = {"qty": ("الكمية", "number"), "rate": ("السعر", "number"), "warehouse": ("المخزن", "link"),
                 "discount_percentage": ("الخصم %", "number")}
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
        raise ApprovalUiError("رمز الموافقة غير صالح. اطلب من المساعد إعادة عرض بطاقة الموافقة.")


def _visible(approval, user):
    if approval.requested_by == user:
        return True
    return ApprovalEngine(PermissionContext(user))._can_decide(approval, user)


def _load(approval_id, user):
    if not approval_id or not frappe.db.exists("AI Approval Request", approval_id):
        raise ApprovalUiError("طلب الموافقة غير موجود.")
    approval = frappe.get_doc("AI Approval Request", approval_id)
    if not _visible(approval, user):
        raise ApprovalUiError("طلب الموافقة غير موجود.")
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


FEMININE = {"Sales Invoice", "Purchase Invoice", "Quotation"}
PARTY_DOCTYPES = {"Sales Invoice": ("customer", "العميل"), "Sales Order": ("customer", "العميل"), "Quotation": ("party_name", "العميل"), "Delivery Note": ("customer", "العميل"),
                  "Purchase Invoice": ("supplier", "المورد"), "Purchase Order": ("supplier", "المورد"), "Purchase Receipt": ("supplier", "المورد")}
DEFINITE = {"Sales Invoice": "الفاتورة", "Purchase Invoice": "الفاتورة", "Delivery Note": "إذن التسليم", "Sales Order": "أمر البيع", "Purchase Order": "أمر الشراء"}


def _preview(action_type, doctype, payload):
    """Validate and preview without saving anything."""
    warnings, fields, items, kpis = [], [], [], []
    empty = {"fields": fields, "items": items, "kpis": kpis, "warnings": warnings, "valid": True, "photo": None, "blocked": None,
             "impact": _impact(action_type, doctype, {}, [], None, None), "details": []}
    if action_type != "Create" or not doctype or not isinstance(payload, dict):
        return empty
    doc, valid = None, True
    try:
        doc = frappe.get_doc({**payload, "doctype": doctype})
        for method in ("set_missing_values", "calculate_taxes_and_totals"):
            if hasattr(doc, method):
                getattr(doc, method)()
        doc.run_method("validate")
    except Exception as exc:
        valid = False
        warnings.append(schema.warning("critical", _plain_error(exc), "validation"))
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
        kpis.append(schema.kpi("grand_total", "الإجمالي", total, currency=currency, tone="info"))
    fields.extend(_facts(doctype, source, items, currency, total))
    stock_warnings, blocked = _stock_check(doctype, items, source)
    warnings += stock_warnings
    photo = next((thumbnail_data_uri(i["image"]) for i in items if i["image"]), None)
    return {"fields": fields, "items": items, "kpis": kpis, "warnings": warnings, "valid": valid, "currency": currency, "photo": photo,
            "blocked": _blocked(doctype, blocked, source) if blocked else None,
            "impact": _impact(action_type, doctype, source, items, currency, total), "details": _details(doctype, payload)}


def _details(doctype, payload):
    """Everything the request will write, as labelled values ("عرض التفاصيل"): what was asked for, nothing computed."""
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
    facts = [{"key": "party", "label": party_label, "d": str(party or "—"), "icon": "user", "link": True}]
    if len(items) == 1:
        it = items[0]
        unit = f" {it['uom']}" if it.get("uom") else ""
        chip = None
        if it["stock"] is not None:
            if it["stock"]["ok"]:
                chip = {"d": "متوفر في المخزن", "tone": "success"}
            elif deducts_stock(doctype, source):
                chip = {"d": "المتاح أقل من المطلوب", "tone": "danger"}
            else:       # the document does not move stock: availability is a note, not a problem
                chip = {"d": f"المتاح {fmt.number(it['stock']['available'])}", "tone": "neutral"}
        facts += [{"key": "qty", "label": "الكمية", "d": f"{it['d']['qty']}{unit}", "icon": "hash"},
                  {"key": "item", "label": "الصنف", "d": str(it["item"] or "—"), "icon": "package", "link": True, "chip": chip},
                  {"key": "rate", "label": f"سعر {it['uom'] or 'الوحدة'}", "d": it["d"]["rate"], "icon": "coins"}]
    payment = source.get("mode_of_payment") or next((p.get("mode_of_payment") for p in (source.get("payments") or []) if isinstance(p, dict) and p.get("mode_of_payment")), None)
    if payment:
        facts.append({"key": "payment", "label": "طريقة الدفع", "d": str(payment), "icon": "card"})
    if total not in (None, ""):
        facts.append({"key": "total", "label": "الإجمالي", "d": fmt.money(total, currency), "icon": "receipt"})
    if source.get("company"):
        facts.append({"key": "company", "label": "الشركة", "d": str(source.get("company")), "icon": "building", "link": True})
    if source.get("due_date") and doctype in ("Sales Invoice", "Purchase Invoice"):
        facts.append({"key": "due", "label": "الاستحقاق", "d": fmt.date(source.get("due_date")), "icon": "calendar"})
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
            warnings.append(schema.warning("critical", f"المخزون غير كافٍ للصنف {it['item']}: المتاح {fmt.number(stock['available'])}{unit} والمطلوب {fmt.number(it['qty'])}{unit}.", "insufficient_stock"))
            blocked = blocked or it
        else:
            warnings.append(schema.warning("info", f"للعلم: المتاح من {it['item']} في المخزن {fmt.number(stock['available'])}{unit} والمطلوب {fmt.number(it['qty'])}{unit}. "
                                                   "هذه الفاتورة لا تخصم من المخزون، فلا يمنع ذلك إنشاءها.", "stock_below_requested"))
    return warnings, blocked


def _impact(action_type, doctype, source, items, currency, total):
    """What approving will change: money, stock, and what happens next. Facts only, from the prepared document."""
    from ai_agent.presentation import money

    financial, stock, steps = [], [], []
    if total not in (None, ""):
        financial.append({"label": "إجمالي المستند", "d": fmt.money(total, currency)})
        taxes = fmt.to_number(source.get("total_taxes_and_charges"))
        if taxes:
            financial.append({"label": "منها ضرائب ورسوم", "d": fmt.money(taxes, currency)})
        if money.is_multi_currency(source) and fmt.to_number(source.get("base_grand_total")):
            financial.append({"label": "بعملة الشركة", "d": fmt.money(source.get("base_grand_total"), money.company_currency(source.get("company"))),
                              "hint": money.exchange_rate_text(source)})
    if doctype in ("Sales Invoice", "Purchase Invoice", "Delivery Note", "Purchase Receipt", "Stock Entry") and items:
        if deducts_stock(doctype, source):
            direction = "يُخصم من" if doctype in ("Sales Invoice", "Delivery Note") else "يُضاف إلى"
            for it in items[:5]:
                unit = f" {it['uom']}" if it.get("uom") else ""
                stock.append({"label": str(it["item"]), "d": f"{direction} {it.get('warehouse') or 'المخزن الافتراضي'}: {fmt.number(it['qty'])}{unit}"})
        else:
            stock.append({"label": "المخزون", "d": "لا يتأثر: تحديث المخزون غير مفعّل في هذا المستند"})
    if action_type in ("Create", "Bulk Create"):
        steps = ["يُنشأ المستند كمسودة ويأخذ رقمه من ERPNext.", "لا يُرحَّل إلى الحسابات ولا يتأثر أي رصيد قبل اعتماد المسودة.", "نتحقق من وجود المستند ونعرض رابطه."]
    elif action_type == "Submit":
        steps = ["يُعتمد المستند ويُرحَّل إلى دفتر الأستاذ.", "لا يمكن تعديله بعد الاعتماد، بل إلغاؤه فقط.", "نتحقق من حالته بعد التنفيذ."]
    elif action_type == "Cancel":
        steps = ["يُلغى المستند وتُعكس قيوده.", "نتحقق من حالته بعد التنفيذ."]
    elif action_type == "Update":
        steps = ["تُحفظ التعديلات على المستند إن لم يتغير منذ تجهيز الطلب.", "نتحقق من المستند بعد التنفيذ."]
    elif action_type == "Workflow":
        steps = ["يُنفَّذ إجراء سير العمل بصلاحياتك.", "نتحقق من الحالة الجديدة."]
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
    label = DEFINITE.get(doctype) or doctype_label(doctype)
    return {"title": f"لا يمكن اعتماد {label}",
            "message": "الكمية المطلوبة غير متوفرة في المخزن المحدد.", "item": it["item"], "item_code": it["item_code"], "warehouse": it["warehouse"],
            "image": thumbnail_data_uri(it.get("image")), "requested": fmt.to_number(it["qty"]), "available": it["stock"]["available"], "unit": it.get("uom"),
            "alternatives": alternatives}


def _plain_error(exc):
    text = frappe.utils.strip_html(str(exc)).strip()
    return text[:300] or "فشل التحقق من المستند."


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
        out.append({"key": key, "label": label, "state": step_state})
    return out


def build_card(approval, user, *, state=None, result=None, error=None):
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
    action_label = ACTION_LABELS.get(approval.action_type, approval.action_type)
    preview = _preview(approval.action_type, doctype, payload) if state == "pending" else {
        "fields": [], "items": [], "kpis": [], "warnings": [], "valid": True, "photo": None, "blocked": None, "impact": {"financial": [], "stock": [], "next": []}, "details": []}
    preview.setdefault("impact", _impact(approval.action_type, doctype, {}, [], None, None))
    preview.setdefault("details", [])
    if approval.action_type == "Bulk Create":
        preview["fields"].append({"key": "count", "label": "عدد المستندات", "d": fmt.number(len(payload.get("documents") or payload.get("items") or []))})
    if approval.action_type in ("Update", "Submit", "Cancel", "Workflow") and approval.document_name:
        preview["fields"].append({"key": "document", "label": "المستند", "d": f"{type_label} {approval.document_name}"})
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
        preview["warnings"].append(schema.warning("info", "سياسة العينين مفعّلة: لا يمكنك اعتماد طلبك بنفسك. يعتمده مستخدم مخوَّل من ERP، ويمكنك تعديل الطلب أو إلغاؤه من هنا.", "four_eyes"))

    def args(action):
        return {"approval_id": approval.name, "token": issue_token(approval.name, approval.payload_hash, user, action)}

    creating = approval.action_type == "Create" and isinstance(payload, dict)
    bulk = approval.action_type == "Bulk Create" and isinstance(payload, dict)
    documents = _bulk_documents(payload) if bulk and state == "pending" else None
    actions = []
    if can_act:
        approve_label = f"موافقة وإنشاء {DEFINITE.get(doctype) or type_label}" if creating else ("موافقة وإنشاء المستندات" if bulk else "موافقة وتنفيذ")
        cancel = schema.action("cancel", "إلغاء", kind="tool", style="danger", tool="approvals__decide", arguments={**args("reject"), "decision": "reject"}, confirm=True, icon="x")
        edit = schema.action("edit", "تعديل الكمية" if blocked else "تعديل", kind="edit", style="soft", arguments=args("revise"), icon="edit") if creating else None
        if blocked:
            actions = [a for a in (
                schema.action("view_stock", "عرض المخزون", kind="drill", style="primary", tool="erp__stock_balance",
                              arguments={"item_code": blocked["item_code"], "warehouse": blocked["warehouse"]}, icon="box") if blocked.get("warehouse") else None,
                schema.action("choose_warehouse", "اختيار مخزن آخر", kind="choose", style="secondary", icon="refresh") if creating else None,
                edit, cancel) if a]
        else:
            approve = None if own_request_blocked else schema.action("approve", approve_label, kind="tool", style="success", tool="approvals__decide",
                                                                     arguments={**args("approve"), "decision": "approve"}, icon="check")
            actions = [a for a in (approve, edit, cancel) if a]
    doc_name = (result or {}).get("document_name")
    result_doctype = (result or {}).get("doctype") or doctype
    if state in ("executed", "verified") and doc_name:
        actions = [schema.action("open", "فتح المستند", href=doc_href(result_doctype, doc_name), style="primary")]
    warnings = preview["warnings"]
    if any(w["level"] == "critical" for w in warnings) and can_act and not blocked:
        actions = [a for a in actions if a["key"] != "approve"]
    if creating and state == "pending":
        ready = "جاهزة للإنشاء" if doctype in FEMININE else "جاهز للإنشاء"
        title, subtitle, status_label = f"{type_label} — {ready}", f"تم تجهيز {DEFINITE.get(doctype) or type_label} بناءً على طلبك. يرجى مراجعة التفاصيل والموافقة.", "تحتاج موافقة"
    elif documents:
        n = len(documents)
        title, subtitle, status_label = f"إنشاء {n} مستندات — جاهزة للإنشاء" if n > 2 else f"إنشاء مستندين — جاهزان للإنشاء", (approval.action_summary or "يرجى مراجعة المستندات التالية والموافقة عليها دفعة واحدة."), "تحتاج موافقة"
    else:
        title, subtitle, status_label = f"{action_label} {type_label}".strip(), f"طلب رقم {approval.name}", None
        if stored.get("mode") == "direct":
            subtitle = "نُفّذ مباشرة: الموافقة غير مطلوبة في الإعدادات الحالية"
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
        "fallback": {"desk_url": desk, "text": f"للموافقة اكتب «موافق» في المحادثة، أو اعتمد الطلب {approval.name} من ERPNext: {desk}"},
        "currency": preview.get("currency"),
        "developer": {"approval_id": approval.name, "action_type": approval.action_type, "doctype": doctype,
                      "payload_hash": approval.payload_hash, "run": approval.run, "requested_by": approval.requested_by},
    }


def _next_steps(approval, steps):
    from ai_agent.mcp import continuation

    steps = list(steps)
    then = continuation.peek(approval.name) if approval.name else None
    if then:
        steps.append(f"ثم نتابع تلقائيًا هدفك الأصلي: {then['goal']}.")
    return steps


def _editable(payload, doctype):
    fields = []
    for key, (label, kind) in EDITABLE_TOP.items():
        if key in payload:
            fields.append({"key": key, "label": label, "type": kind, "value": payload.get(key)})
    items = []
    for idx, row in enumerate(payload.get("items") or [], start=1):
        if isinstance(row, dict):
            items.append({"idx": idx, "label": row.get("item_name") or row.get("item_code"),
                          "fields": [{"key": k, "label": lab, "type": t, "value": row.get(k)} for k, (lab, t) in EDITABLE_ITEM.items() if k in row]})
    return {"fields": fields, "items": items} if (fields or items) else None


def card_envelope(approval, user, *, tool="approvals", state=None, result=None, error=None):
    card = build_card(approval, user, state=state, result=result, error=error)
    text = f"{card['title']} — {approval.name}: " + {
        "pending": "بانتظار موافقتك", "approved": "تمت الموافقة", "executed": "نُفّذ", "verified": "نُفّذ وتم التحقق",
        "rejected": "رُفض", "expired": "انتهت صلاحيته", "failed": "فشل التنفيذ"}[card["state"]]
    if card["kpis"]:
        text += "، " + "، ".join(f"{k['label']} {k['d']}" for k in card["kpis"][-1:])
    if card["state"] == "pending":
        # Text hosts (and the model) need to know the request can be decided right here in the chat.
        text += f". للموافقة اكتب «موافق» وللإلغاء اكتب «إلغاء» (رقم الطلب {approval.name})"
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
        return schema.envelope(tool="approvals.list_pending", data={"pending": []}, summary={"text": "لا توجد طلبات موافقة معلّقة.", "count": 0})
    if len(rows) == 1:
        approval = frappe.get_doc("AI Approval Request", rows[0].name)
        envelope = card_envelope(approval, user, tool="approvals.list_pending")
        envelope["data"]["pending"] = [rows[0].name]
        return envelope
    from ai_agent.presentation.presenters import choice

    options = [{"value": r.name, "label": (r.action_summary or f"{ACTION_LABELS.get(r.action_type, r.action_type)} {r.doctype_name or ''}").strip()[:140],
                "meta": r.name, "icon": "file"} for r in rows]
    envelope = choice.build("أي طلب تقصد؟", options, subtitle="لديك أكثر من طلب موافقة معلّق. اختر الطلب الذي تريد البتّ فيه.", icon="file",
                            reply_template="أقصد الطلب رقم {value}")
    envelope["data"]["pending"] = [r.name for r in rows]
    return envelope


def decide(user, approval_id, decision, token=None, reason=None):
    """The one place a decision is taken, for the card buttons (signed token) and for the chat (the user typed it).

    Exactly-once: a second "approve" returns the outcome of the first, it never runs the action again.
    """
    via = "app" if token else "chat"
    events.log("approval_decision_received", user=user, approval_id=approval_id, decision=decision, via=via)
    if decision not in ("approve", "reject"):
        raise ApprovalUiError("قرار غير معروف: استخدم approve أو reject.")
    if not approval_required():
        raise ApprovalUiError("الموافقة غير مطلوبة في الإعدادات الحالية؛ تُنفَّذ الطلبات مباشرة.")
    approval = _load(approval_id, user)
    events.log("approval_loaded", user=user, approval_id=approval.name, doctype=approval.doctype_name, status=approval.status)
    if token:
        if not app_approval_enabled():
            raise ApprovalUiError("الموافقة من البطاقة غير مفعلة. اكتب موافقتك في المحادثة أو اعتمد الطلب من واجهة ERPNext.")
        _check_token(approval, user, token, decision)

    # A repeated decision is answered with the recorded outcome, never executed again.
    if approval.status == "Executed" and decision == "approve":
        return card_envelope(approval, user, tool="approvals.decide")
    if approval.status == "Rejected" and decision == "reject":
        return card_envelope(approval, user, tool="approvals.decide", state="rejected")
    if approval.status not in ("Pending", "Approved"):
        raise ApprovalUiError(f"لا يمكن البتّ في هذا الطلب: حالته {_STATUS_AR.get(approval.status, approval.status)}.")

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
                raise ApprovalUiError("انتهت صلاحية طلب الموافقة. اطلب من المساعد إعادة تجهيز العملية.")
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


_STATUS_AR = {"Executed": "نُفّذ", "Rejected": "مرفوض", "Expired": "منتهي الصلاحية", "Failed": "فشل تنفيذه", "Approved": "معتمد", "Pending": "معلّق"}


def _outcome(user, approval_name):
    approval = frappe.get_doc("AI Approval Request", approval_name)
    return card_envelope(approval, user, tool="approvals.decide")


def record_verification(user, approval, result, *, via=None, mode=None):
    """Re-read what the action claims it changed and keep the verdict with the execution result."""
    events.log("approval_verify_started", user=user, approval_id=approval.name, doctype=approval.doctype_name)
    ok = _verify(result)
    stored = _stored_result(approval) or (result if isinstance(result, dict) else {})
    stored = {**stored, "verified": ok, "verified_at": str(frappe.utils.now_datetime()), "approved_via": via}
    if mode:
        stored["mode"] = mode
    frappe.db.set_value("AI Approval Request", approval.name, "execution_result", json.dumps(stored, ensure_ascii=False, default=str), update_modified=False)
    frappe.db.commit()
    events.log("approval_verified" if ok else "approval_failed", user=user, approval_id=approval.name, doctype=approval.doctype_name, verified=ok)
    return ok


def _verify(result):
    """Re-read what the approval claims it created."""
    if not isinstance(result, dict):
        return False
    if result.get("document_name") and result.get("doctype"):
        return bool(frappe.db.exists(result["doctype"], result["document_name"]))
    docs = result.get("documents") or []
    return bool(docs) and all(frappe.db.exists(d.get("doctype") or result.get("doctype"), d.get("document_name")) for d in docs if isinstance(d, dict))


def revise(user, approval_id, token, changes):
    """Edit a pending Create before approval: same checks as a fresh request, old request withdrawn."""
    approval = _load(approval_id, user)
    _check_token(approval, user, token, "revise")
    if approval.status != "Pending" or approval.action_type != "Create":
        raise ApprovalUiError("لا يمكن تعديل هذا الطلب.")
    payload = copy.deepcopy(json.loads(approval.payload_json))
    for key, value in ((changes or {}).get("fields") or {}).items():
        allowed_new = key == "set_warehouse" and frappe.get_meta(approval.doctype_name).has_field("set_warehouse")
        if key not in EDITABLE_TOP or (key not in payload and not allowed_new):
            raise ApprovalUiError(f"الحقل غير قابل للتعديل: {key}")
        payload[key] = value
        if key == "set_warehouse":
            for row in payload.get("items") or []:
                if isinstance(row, dict):
                    row["warehouse"] = value
    for idx, edits in ((changes or {}).get("items") or {}).items():
        try:
            row = payload["items"][int(idx) - 1]
        except (KeyError, IndexError, ValueError, TypeError):
            raise ApprovalUiError("صف غير موجود.")
        for key, value in (edits or {}).items():
            if key not in EDITABLE_ITEM or key not in row:
                raise ApprovalUiError(f"الحقل غير قابل للتعديل: {key}")
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
