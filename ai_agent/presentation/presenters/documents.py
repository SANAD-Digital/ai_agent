"""Single-document view (data.get_document).

The document is mapped before it is shown: fields are sorted into business sections, the most important ones first,
amounts carry the currency ERPNext declares for them, equal or empty values are dropped, and everything secondary
goes behind "تفاصيل إضافية". Child tables get business columns, never the row's technical id.
"""

import frappe

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import money, schema
from ai_agent.presentation.labels import TECHNICAL, doctype_label, field_tier, label_for, status_display, translate
from ai_agent.presentation.registry import register
from ai_agent.presentation.urls import doc_href

TYPE_BY_FIELDTYPE = {
    "Currency": "currency", "Float": "number", "Int": "number", "Percent": "percentage",
    "Date": "date", "Datetime": "datetime", "Check": "boolean",
}
SKIP_TYPES = {"Text Editor", "HTML Editor", "HTML", "Attach", "Attach Image", "Code", "JSON", "Button", "Signature", "Geolocation"}
PARTY_NAME_OF = {"customer": "customer_name", "supplier": "supplier_name", "party": "party_name"}

# Section -> ordered fields. A field appears in the first section that names it.
SECTIONS = [
    ("basic", "المعلومات الأساسية", "file", [
        "company", "posting_date", "transaction_date", "due_date", "delivery_date", "schedule_date", "bill_no", "bill_date",
        "po_no", "po_date", "voucher_type", "payment_type", "item_name", "item_group", "stock_uom", "customer_type", "supplier_type"]),
    ("party", "الطرف", "user", [
        "customer", "supplier", "party_type", "party", "customer_group", "supplier_group", "territory", "tax_id",
        "contact_display", "contact_mobile", "contact_email"]),
    ("amounts", "المبالغ", "coins", [
        "currency", "conversion_rate", "total", "net_total", "discount_amount", "total_taxes_and_charges", "grand_total", "rounded_total",
        "base_grand_total", "total_advance", "paid_amount", "received_amount", "base_paid_amount", "base_received_amount",
        "total_allocated_amount", "unallocated_amount", "outstanding_amount", "write_off_amount", "total_debit", "total_credit", "difference"]),
    ("status", "الحالة", "checkCircle", ["status", "workflow_state", "is_return", "is_pos", "is_opening", "is_paid", "disabled", "is_frozen"]),
    ("stock", "المخزون", "boxes", ["update_stock", "set_warehouse", "set_target_warehouse", "total_qty", "total_net_weight", "is_stock_item", "valuation_method"]),
    ("payment", "الدفع", "card", [
        "mode_of_payment", "payment_terms_template", "cash_bank_account", "paid_from", "paid_to", "reference_no", "reference_date"]),
    ("accounting", "المحاسبة", "scale", [
        "debit_to", "credit_to", "against_income_account", "cost_center", "project", "party_account_currency",
        "selling_price_list", "buying_price_list"]),
    ("taxes", "الضرائب", "percent", ["tax_category", "taxes_and_charges", "base_total_taxes_and_charges"]),
]
SECTION_OF = {field: key for key, _, _, fields in SECTIONS for field in fields}
# Shown even when 0 / "لا": the user must see that stock is, or is not, affected, and what is still owed.
ALWAYS_SHOW = {"update_stock", "outstanding_amount"}
# The same figure at different stages; when two are equal only the more final one is shown.
AMOUNT_CHAIN = ["grand_total", "rounded_total", "net_total", "total"]

# Child tables: business columns first; the rest is available in the row's details.
ITEM_COLUMNS = ["item_name", "item_code", "qty", "uom", "rate", "amount", "warehouse"]
TABLE_COLUMNS = {
    "accounts": ["account", "party", "debit", "credit", "cost_center", "user_remark"],
    "taxes": ["description", "account_head", "rate", "tax_amount", "total"],
    "payments": ["mode_of_payment", "amount", "account"],
    "references": ["reference_doctype", "reference_name", "total_amount", "outstanding_amount", "allocated_amount"],
    "payment_schedule": ["due_date", "payment_term", "invoice_portion", "payment_amount", "outstanding"],
}
TABLE_TITLES = {"items": "الأصناف", "taxes": "الضرائب", "accounts": "القيود", "payments": "المدفوعات", "references": "المستندات المرتبطة",
                "payment_schedule": "جدول السداد", "uoms": "وحدات القياس", "advances": "الدفعات المقدمة", "sales_team": "فريق المبيعات"}
TOTALLED = ("amount", "debit", "credit", "tax_amount", "allocated_amount", "net_amount", "payment_amount")
MAX_DETAIL_COLUMNS = 10
VISIBLE_COLUMNS = 6
MAX_MORE_FIELDS = 40


def _kind(field, key, value, doctype):
    kind = TYPE_BY_FIELDTYPE.get(field.fieldtype if field else "")
    if kind:
        return kind
    if key in ("status", "workflow_state"):
        return "status"
    if field and field.fieldtype in ("Link", "Dynamic Link", "Data", "Select", "Small Text", "Text", "Long Text", "Read Only"):
        return "text"
    return schema.infer_column(key, [value], doctype)["type"]


def _meaningful(key, value, field):
    """False for values that say nothing: blank, unchecked boxes, zero amounts, rich text."""
    if field and field.fieldtype in SKIP_TYPES:
        return False
    if key in ALWAYS_SHOW:
        return value is not None
    if fmt.is_blank(value):
        return False
    if field and field.fieldtype == "Check":
        return bool(frappe.utils.cint(value))
    if field and field.fieldtype == "Select" and str(value).strip().lower() in ("no", "none"):
        return False
    if field and field.fieldtype in ("Currency", "Float", "Int", "Percent"):
        return bool(fmt.to_number(value))
    if isinstance(value, str) and "<" in value and ">" in value:
        return bool(frappe.utils.strip_html(value).strip()) and len(value) < 400
    return True


def _display(key, value, kind, field, doc, parent=None):
    if kind == "currency":
        return fmt.money(value, money.field_currency(field, doc, parent))
    if kind == "status":
        return status_display(value)[0]
    fieldtype = field.fieldtype if field else None
    if fieldtype == "Percent":
        return fmt.percent_value(value)
    if fieldtype == "Time":
        return fmt.time_text(value)
    if fieldtype == "Select" and isinstance(value, str):
        return translate(value)          # option values are shown in the user's language, stored as they are
    if fieldtype == "Link" and field.options == "DocType" and isinstance(value, str):
        return doctype_label(value)
    if isinstance(value, str) and "<" in value:
        value = frappe.utils.strip_html(value).strip()
    return schema.cell(value, schema.column(key, "x", kind))["d"]


def _field(key, value, field, doctype, doc):
    kind = _kind(field, key, value, doctype)
    item = {"key": key, "label": label_for(key, doctype), "type": kind, "v": value, "d": _display(key, value, kind, field, doc)}
    if kind == "status":
        item["tone"] = status_display(value)[1]
    return item


def _redundant(doc, keys):
    """(fields to drop, fields to move to the details): amounts that repeat a more final figure, and
    company-currency twins when the document has one currency only."""
    drop, demote, seen = set(), set(), {}
    for key in AMOUNT_CHAIN:
        if key in keys and fmt.to_number(doc.get(key)) is not None:
            value = round(fmt.to_number(doc.get(key)), 2)
            if value in seen:
                drop.add(key)
            else:
                seen[value] = key
    multi = money.is_multi_currency(doc)
    for key in keys:
        if key.startswith("base_") and key[5:] in doc:
            if not multi:
                drop.add(key)
            elif key not in ("base_grand_total", "base_paid_amount", "base_received_amount"):
                demote.add(key)
    if not multi:
        drop.update({"conversion_rate", "party_account_currency", "plc_conversion_rate", "price_list_currency",
                     "source_exchange_rate", "target_exchange_rate", "paid_from_account_currency", "paid_to_account_currency"})
    return drop, demote


def build_document(doctype, name, result):
    """The document view model: header, sections, child tables, details."""
    meta = frappe.get_meta(doctype)
    doc = {k: v for k, v in result.items() if not isinstance(v, (list, dict))}
    keys = [k for k in result if k not in ("name", "doctype")]
    drop, demote = _redundant(doc, keys)
    for id_key, name_key in PARTY_NAME_OF.items():       # one line per party
        if doc.get(id_key) and doc.get(name_key):
            drop.add(name_key)

    buckets = {key: [] for key, *_ in SECTIONS}
    more, technical, tables = [], [], []
    for key in keys:
        value, field = result[key], meta.get_field(key)
        if isinstance(value, list):
            if field and field.options and value and isinstance(value[0], dict):
                table = child_table(field.options, key, value, doc, title=TABLE_TITLES.get(key) or label_for(key, doctype))
                if table:
                    tables.append(table)
            continue
        if key in TECHNICAL or key.startswith("_") or field_tier(key) == 4:
            if not fmt.is_blank(value):
                technical.append({"label": key, "d": fmt.text(value)})
            continue
        if key in drop or not _meaningful(key, value, field):
            continue
        item = _field(key, value, field, doctype, doc)
        if key in PARTY_NAME_OF and doc.get(PARTY_NAME_OF[key]):
            item["d"] = str(doc[PARTY_NAME_OF[key]])
            if str(value) != item["d"]:
                more.append({"key": key + "_id", "label": f"{item['label']} (الرمز)", "type": "text", "v": value, "d": str(value)})
        if key in SECTION_OF and key not in demote:
            buckets[SECTION_OF[key]].append(item)
        else:
            more.append(item)

    rate = money.exchange_rate_text(doc)
    for item in buckets["amounts"]:
        if item["key"] == "conversion_rate" and rate:
            item["d"] = rate

    sections = []
    for key, title, icon, order in SECTIONS:
        fields = sorted(buckets[key], key=lambda f: order.index(f["key"]))
        if key == "status":
            fields = [f for f in fields if f["key"] != "status"]      # the status is the header badge
        if fields:
            sections.append({"key": key, "title": title, "icon": icon, "fields": fields})
    if more:
        sections.append({"key": "more", "title": "تفاصيل إضافية", "icon": "more", "collapsed": True, "fields": more[:MAX_MORE_FIELDS]})

    status = doc.get("status")
    if not status and meta.is_submittable:
        docstatus = result.get("docstatus")
        if docstatus is None:
            docstatus = frappe.db.get_value(doctype, name, "docstatus")     # the caller has already been allowed to read this document
        status = {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(frappe.utils.cint(docstatus))
    party = next((str(doc[n]) for n in ("customer_name", "supplier_name", "party_name", "customer", "supplier", "party") if doc.get(n)), None)
    headline = next((k for k in ("grand_total", "paid_amount", "total_debit", "total") if fmt.to_number(doc.get(k))), None)
    kpis = []
    if headline:
        kpis.append(schema.kpi(headline, "الإجمالي" if headline == "grand_total" else label_for(headline, doctype), doc[headline],
                               currency=money.field_currency(meta.get_field(headline), doc), icon="receipt", tone="info"))
        if money.is_multi_currency(doc) and fmt.to_number(doc.get("base_" + headline)):
            kpis.append(schema.kpi("base_" + headline, "بعملة الشركة", doc["base_" + headline],
                                   currency=money.company_currency(doc.get("company")), icon="coins", hint=rate))
        if fmt.to_number(doc.get("outstanding_amount")):
            kpis.append(schema.kpi("outstanding_amount", "المستحق", doc["outstanding_amount"],
                                   currency=money.field_currency(meta.get_field("outstanding_amount"), doc), icon="clockAlert", tone="warning"))
    presentation = {
        "type": "document", "title": f"{doctype_label(doctype)} {name}", "subtitle": party, "icon": "file",
        "status": {"d": status_display(status)[0], "tone": status_display(status)[1]} if status else None,
        "sections": sections, "tables": tables, "kpis": kpis, "warnings": [], "insights": [],
        "actions": [schema.action("erp", "فتح في ERP", href=doc_href(doctype, name), style="primary", icon="external"),
                    schema.action("copy", "نسخ الرقم", kind="copy", arguments={"text": str(name)}, overflow=True)],
        "developer": {"doctype": doctype, "name": name, "technical_fields": technical},
    }
    return presentation, party, doc


def child_table(child_doctype, parentfield, rows, parent, title=None):
    """A child table with business columns: '#', the columns that matter, then details behind the row expander."""
    meta = frappe.get_meta(child_doctype)
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return None
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    preferred = TABLE_COLUMNS.get(parentfield) or (ITEM_COLUMNS if "item_code" in keys else [])
    multi = money.is_multi_currency(parent)

    def usable(key):
        field = meta.get_field(key)
        if key in TECHNICAL or key in ("name", "idx", "doctype") or key.startswith("_") or (field and field.fieldtype in SKIP_TYPES):
            return False
        if key.startswith("base_") and key[5:] in keys and not multi:
            return False
        if key == "item_code" and "item_name" in keys and all(str(r.get("item_code")) == str(r.get("item_name")) for r in rows):
            return False
        return any(_meaningful(key, r.get(key), field) for r in rows)      # a column nobody filled is not a column

    def twin_of(key):
        for suffix in ("_in_account_currency", "_in_transaction_currency"):
            if key.endswith(suffix) and key[: -len(suffix)] in keys:
                return key[: -len(suffix)], " (عملة الحساب)" if "account" in suffix else " (عملة المعاملة)"
        return None, ""

    def repeats_twin(key):
        twin, _ = twin_of(key)
        return bool(twin) and all((fmt.to_number(r.get(key)) or 0) == (fmt.to_number(r.get(twin)) or 0) for r in rows)

    ordered = [k for k in preferred if k in keys and usable(k)]
    rest, _ = schema.order_keys([k for k in keys if k not in ordered and usable(k) and not repeats_twin(k)])
    ordered += rest
    currency_fields = {f.fieldname for f in meta.fields if f.fieldtype == "Currency"}
    columns = [schema.column("_index", "#", "number", sortable=False, width="44px")]
    for position, key in enumerate(ordered):
        field = meta.get_field(key)
        kind = "currency" if key in currency_fields else _kind(field, key, next((r.get(key) for r in rows if not fmt.is_blank(r.get(key))), None), child_doctype)
        twin, suffix = twin_of(key)
        col = schema.column(key, label_for(twin, child_doctype) + suffix if twin else label_for(key, child_doctype), kind,
                            total=kind == "currency" and key in TOTALLED)
        col["priority"] = "primary" if position == 0 else "secondary" if position < 3 else "supporting"
        if position >= VISIBLE_COLUMNS or (preferred and key not in preferred):
            col["collapsed"] = True
        columns.append(col)
    columns = [c for c in columns if not c.get("collapsed")] + [c for c in columns if c.get("collapsed")][:MAX_DETAIL_COLUMNS]

    out, totals = [], {}
    for index, row in enumerate(rows, start=1):
        cells = {"_index": {"v": index, "d": str(index)}}
        for col in columns[1:]:
            field, value = meta.get_field(col["key"]), row.get(col["key"])
            if not _meaningful(col["key"], value, field) and not (col["type"] in ("currency", "number") and not col.get("collapsed") and value is not None):
                cells[col["key"]] = {"v": value, "d": "—"}
                continue
            cell = {"v": value, "d": _display(col["key"], value, col["type"], field, row, parent)}
            if col["type"] == "status":
                cell["tone"] = status_display(value)[1]
            cells[col["key"]] = cell
            if col.get("total"):
                entry = totals.setdefault(col["key"], {"sum": 0.0, "currencies": set()})
                entry["sum"] += fmt.to_number(value) or 0
                entry["currencies"].add(money.field_currency(field, row, parent))
        out.append(schema.make_row(cells, id=str(index)))
    # A total is printed only when every row is in the same currency.
    footer = {k: {"v": t["sum"], "d": fmt.money(t["sum"], next(iter(t["currencies"])))} for k, t in totals.items() if len(t["currencies"]) == 1 and len(rows) > 1}
    return schema.table(
        title or label_for(parentfield), columns, out, variant="data", kpis=[], actions=[],
        totals={"label": "الإجمالي", "cells": footer} if footer else None,
        paging={"total": len(out), "shown": len(out), "offset": 0, "noun": "سطر"}, density="compact", sort=None)


@register("data.get_document")
def present_document(tool, arguments, result, ctx):
    doctype = result.get("doctype") or arguments.get("doctype")
    name = result.get("name") or arguments.get("name")
    presentation, party, doc = build_document(doctype, name, result)
    text = presentation["title"] + (f" — {party}" if party else "")
    if presentation["status"]:
        text += f" — {presentation['status']['d']}"
    for k in presentation["kpis"]:
        text += f" — {k['label']} {k['d']}"
    return schema.envelope(
        tool=tool, summary={"text": text},
        data={"doctype": doctype, "name": name, "status": doc.get("status"), "sections": [s["title"] for s in presentation["sections"]],
              "tables": [t["title"] for t in presentation["tables"]]},
        presentation=presentation)
