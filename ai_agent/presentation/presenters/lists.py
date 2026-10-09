"""Row-set presenters: data.query, artifacts.create_table and data.count.

Domain views (`VIEWS`) fix column order, labels, sort and KPIs per DocType.
Anything else goes through the generic presenter, which still hides technical
fields, orders by priority tier and types columns.
"""

import re

import frappe

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.labels import DOCTYPE_PLURALS, humanize, label_for, status_display
from ai_agent.presentation.registry import register
from ai_agent.presentation.urls import doc_href, slug

CURRENCY_KEYS = ("currency", "default_currency", "account_currency", "transaction_currency", "party_account_currency")
UNSET_WHEN_ZERO = {"capital", "paid_up_capital", "total_number_of_shares"}
AGG_PREFIX = re.compile(r"^(sum|count|avg|min|max|total)_(.+)$")
AGG_WORD = {"sum": "إجمالي", "count": "عدد", "avg": "متوسط", "min": "أدنى", "max": "أعلى", "total": "إجمالي"}

INVOICE_KPIS = (("grand_total", "إجمالي الفواتير"), ("outstanding_amount", "المستحق"))

VIEWS = {
    "Company": {
        "variant": "data", "name_label": "اسم الشركة", "order": ["name", "default_currency", "capital", "country"],
        "sort": ("name", "asc"), "icon": "building", "noun": "شركات", "index": True, "new_label": "شركة جديدة",
        "search": "بحث في الشركات", "enrich": "company_capital",
    },
    "Sales Invoice": {
        "icon": "receipt", "noun": "فاتورة", "new_label": "فاتورة جديدة", 
        "variant": "data", "name_label": "الفاتورة",
        "order": ["name", "customer_name", "customer", "posting_date", "due_date", "grand_total", "outstanding_amount", "status"],
        "sort": ("posting_date", "desc"), "kpis": INVOICE_KPIS, "party": "customer",
    },
    "Purchase Invoice": {
        "icon": "receipt", "noun": "فاتورة", "new_label": "فاتورة جديدة", 
        "variant": "data", "name_label": "الفاتورة",
        "order": ["name", "supplier_name", "supplier", "posting_date", "due_date", "grand_total", "outstanding_amount", "status"],
        "sort": ("posting_date", "desc"), "kpis": INVOICE_KPIS, "party": "supplier",
    },
    "Payment Entry": {
        "icon": "wallet", "noun": "سند", "new_label": "سند جديد", 
        "variant": "data", "name_label": "السند",
        "order": ["name", "party_name", "party", "posting_date", "mode_of_payment", "paid_amount", "status"],
        "sort": ("posting_date", "desc"), "kpis": (("paid_amount", "إجمالي المدفوعات"),),
    },
    "Journal Entry": {
        "icon": "file", "noun": "قيد", "new_label": "قيد جديد", 
        "variant": "gl", "name_label": "القيد",
        "order": ["name", "posting_date", "voucher_type", "remarks", "total_debit", "total_credit"],
        "sort": ("posting_date", "desc"),
    },
    "GL Entry": {
        "icon": "file", "noun": "قيد", 
        "variant": "gl", "name_label": "القيد",
        "order": ["posting_date", "voucher_no", "voucher_type", "account", "remarks", "debit", "credit", "balance"],
        "sort": ("posting_date", "desc"), "kpis": (("debit", "إجمالي المدين"), ("credit", "إجمالي الدائن")),
    },
    "Customer": {
        "icon": "users", "noun": "عميل", "new_label": "عميل جديد", "variant": "data", "name_label": "العميل", "order": ["name", "customer_name", "customer_group", "territory", "outstanding_amount", "disabled"], "sort": ("name", "asc")},
    "Supplier": {
        "icon": "users", "noun": "مورد", "new_label": "مورد جديد", "variant": "data", "name_label": "المورد", "order": ["name", "supplier_name", "supplier_group", "outstanding_amount", "disabled"], "sort": ("name", "asc")},
    "Item": {
        "icon": "package", "noun": "صنف", "new_label": "صنف جديد", "variant": "data", "name_label": "الصنف", "order": ["name", "item_name", "item_group", "stock_uom", "disabled"], "sort": ("name", "asc")},
    "Bin": {
        "icon": "boxes", "noun": "سجل", "variant": "stock", "name_label": "السجل", "order": ["item_code", "warehouse", "actual_qty", "reserved_qty", "projected_qty", "valuation_rate", "stock_value"], "sort": ("actual_qty", "asc")},
    "Stock Ledger Entry": {
        "icon": "boxes", "noun": "حركة", "variant": "stock", "name_label": "الحركة", "order": ["posting_date", "item_code", "warehouse", "actual_qty", "qty_after_transaction", "voucher_no"], "sort": ("posting_date", "desc")},
}
EXTRA_LABELS = {
    "total_debit": "مدين", "total_credit": "دائن", "disabled": "معطّل", "item_group": "مجموعة الأصناف",
    "stock_uom": "الوحدة", "qty_after_transaction": "الرصيد بعد الحركة",
}


def _spec(arguments):
    spec = arguments.get("specification")
    return spec if isinstance(spec, dict) else arguments


def _alias_label(key, doctype):
    if key in EXTRA_LABELS:
        return EXTRA_LABELS[key]
    match = AGG_PREFIX.match(key)
    if match and match.group(1) in AGG_WORD:
        base = label_for(match.group(2), doctype)
        return base if match.group(1) == "total" else f"{AGG_WORD[match.group(1)]} {base}"
    return label_for(key, doctype)


def _currency_key(keys):
    return next((k for k in CURRENCY_KEYS if k in keys), None)


def _default_currency(spec):
    company = None
    for flt in spec.get("filters") or []:
        if isinstance(flt, dict) and flt.get("field") == "company" and isinstance(flt.get("value"), str):
            company = flt["value"]
    try:
        if company:
            return frappe.get_cached_value("Company", company, "default_currency")
        return frappe.defaults.get_global_default("currency") or frappe.defaults.get_defaults().get("currency")
    except Exception:
        return None


def _company_capital(company, user):
    """Paid-in capital from the ledger, or None when nothing is recorded: unknown must not read as zero."""
    try:
        if not frappe.has_permission("GL Entry", "read", user=user):
            return None
        accounts = [a for a in frappe.get_all("Account", filters={"company": company, "is_group": 0, "account_type": "Equity"}, fields=["name", "account_name"])
                    if any(w in (a.account_name or "").lower() for w in ("capital", "رأس المال", "رأس مال"))]
        if not accounts:
            return None
        totals = frappe.get_all("GL Entry", filters={"account": ["in", [a.name for a in accounts]], "is_cancelled": 0},
                                fields=[{"SUM": "credit", "as": "credit"}, {"SUM": "debit", "as": "debit"}, {"COUNT": "name", "as": "entries"}])
        row = totals[0] if totals else None
        if not row or not row.get("entries"):
            return None
        return (row.get("credit") or 0) - (row.get("debit") or 0)
    except Exception:
        return None


ENRICHERS = {"company_capital": ("capital", lambda row, user: _company_capital(row.get("name"), user) if row.get("name") else None)}


def _enrich(rows, view):
    """Adds server-computed columns the DocType itself does not have (only from data the user can read)."""
    spec = ENRICHERS.get(view.get("enrich"))
    if not spec:
        return rows
    key, compute = spec
    return [row if key in row else {**row, key: compute(row, frappe.session.user)} for row in rows]


def build_rows_view(rows, *, doctype=None, spec=None, title=None, total_count=None, tool_args=None, view=None):
    """Turn a list of dict rows into a table presentation with the domain view applied."""
    spec = spec or {}
    view = view or VIEWS.get(doctype, {})
    rows = _enrich([r for r in rows if isinstance(r, dict)], view)
    keys = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)

    preferred = [k for k in view.get("order", []) if k in keys]
    # Prefer the party's display name over its id when both are present.
    if "customer_name" in preferred and "customer" in preferred:
        preferred.remove("customer")
    if "supplier_name" in preferred and "supplier" in preferred:
        preferred.remove("supplier")
    rest, technical = schema.order_keys([k for k in keys if k not in preferred and k not in view.get("order", [])])
    ordered = preferred + rest
    currency_key = _currency_key(keys)
    currencies = sorted({str(r[currency_key]) for r in rows if currency_key and r.get(currency_key)})
    mixed_currency = len(currencies) > 1
    uniform_currency = currencies[0] if len(currencies) == 1 else (None if currency_key else _default_currency(spec))

    columns = []
    for key in ordered:
        if key == currency_key and not mixed_currency and currencies and key != "name":
            # One currency for the whole table: shown in the amounts, not as a column.
            if key not in view.get("order", []):
                continue
        col = schema.infer_column(key, [r.get(key) for r in rows], doctype)
        if key in UNSET_WHEN_ZERO and col["type"] == "text":
            col["type"], col["align"] = ("number" if key == "total_number_of_shares" else "currency"), "end"
        col["label"] = view.get("name_label") if key == "name" and view.get("name_label") else _alias_label(key, doctype)
        if key in (currency_key,):
            col["type"] = "badge"
            col["label"] = "العملة"
        if col["type"] == "currency":
            col["currency_key"] = currency_key if mixed_currency else None
        if mixed_currency and col.get("total"):
            col.pop("total", None)
        columns.append(col)
    if columns:
        columns[0]["priority"] = "primary"
        for col in columns[1:3]:
            col["priority"] = "secondary"
    # Beyond six columns the rest fold into an expandable detail (and a "more" section on mobile).
    for col in columns[6:]:
        if col["priority"] != "primary" and col["key"] != "_rank":
            col["collapsed"] = True
    ranking = _is_ranking(columns, rows, spec)
    if ranking:
        columns, rows = _add_ranking(columns, rows)
    elif view.get("index"):
        columns.insert(0, schema.column("_index", "#", "number", sortable=False, width="44px"))

    unset_keys = UNSET_WHEN_ZERO
    out_rows = []
    for index, row in enumerate(rows):
        cells = {}
        for col in columns:
            key = col["key"]
            if key in ("_rank", "_index"):
                cells[key] = {"v": index + 1, "d": str(index + 1)}
                continue
            raw = row.get(key)
            if key == "_share":
                cells[key] = schema.cell(raw, col)
                continue
            currency = (row.get(currency_key) if mixed_currency and currency_key else uniform_currency)
            cells[key] = schema.cell(raw, col, currency=currency, zero_as_unset=key in unset_keys)
        name = row.get("name") or row.get("voucher_no")
        link_doctype = doctype if row.get("name") else row.get("voucher_type")
        actions = []
        if name and link_doctype:
            actions = [
                schema.action("open", "فتح", kind="drill", style="primary", overflow=True,
                              tool="data__get_document", arguments={"doctype": link_doctype, "name": name}),
                schema.action("erp", "فتح في ERP", kind="link", href=doc_href(link_doctype, name), overflow=True),
                schema.action("copy", "نسخ الرقم", kind="copy", arguments={"text": str(name)}, overflow=True),
            ]
        out_rows.append(schema.make_row(
            cells, id=str(name) if name else None,
            href=doc_href(link_doctype, name) if name and link_doctype else None, actions=actions))
        if row.get("_tone"):
            out_rows[-1]["tone"] = row["_tone"]

    sort = view.get("sort")
    shown = len(out_rows)
    total = total_count if total_count is not None else shown
    totals, kpis, warnings = None, [], []
    money_cols = [c for c in columns if c["type"] == "currency"]
    if mixed_currency and any(any(r.get(c["key"]) not in (None, "") for r in rows) for c in money_cols):
        warnings.append(schema.warning("info", f"الجدول بعملات متعددة ({'، '.join(currencies)}): لم تُجمع الإجماليات عبر العملات.", "mixed_currency"))
    elif out_rows and money_cols:
        sums = {c["key"]: sum(fmt.to_number(r.get(c["key"])) or 0 for r in rows) for c in money_cols if c.get("total")}
        if sums and not ranking and shown == total:
            totals = {"label": "الإجمالي", "cells": {k: {"v": v, "d": fmt.money(v, uniform_currency)} for k, v in sums.items()}}
        for key, label in view.get("kpis", ()):
            if key in sums:
                kpis.append(schema.kpi(key, label, sums[key], currency=uniform_currency,
                                       hint=None if shown == total else "من الصفوف المعروضة",
                                       filter={"col": key, "gt": 0} if key == "outstanding_amount" else None,
                                       tone="warning" if key == "outstanding_amount" and sums[key] > 0 else "neutral"))
    if total > shown:
        warnings.append(schema.warning("info", f"عرض {fmt.number(shown)} من {fmt.number(total)} — ضيّق الفلاتر أو انتقل للصفحة التالية.", "truncated"))

    label = DOCTYPE_PLURALS.get(doctype) or (title or humanize(doctype))
    summary = {"records": total, "shown": shown}
    if kpis and kpis[0]["key"]:
        summary[kpis[0]["key"]] = kpis[0]["v"]
    developer = {"doctype": doctype, "hidden_fields": technical, "spec": _safe_spec(spec)}
    presentation = schema.table(
        title or label, columns, out_rows,
        variant="ranking" if ranking else view.get("variant", "data"),
        subtitle=f"{fmt.number(total)} {view.get('noun', 'سجل')}" if total and not view.get("noun") else None,
        summary=summary, kpis=kpis, totals=totals, warnings=warnings, sort=sort,
        empty=_empty(doctype),
        paging={"total": total, "total_records": total, "shown": shown, "offset": int(spec.get("offset") or 0), "noun": view.get("noun", "نتيجة")},
        developer=developer, currency=uniform_currency,
        density="compact",
        filters=_filters(columns, rows, doctype),
        icon=view.get("icon"),
        search_placeholder=view.get("search") or (f"بحث في {label}" if doctype else None),
        actions=_header_actions(doctype, view),
    )
    return presentation, rows, uniform_currency


def _select_options(doctype, key, rows):
    """Every value a Select field can take (so the filter is complete), else what the rows contain."""
    raw = []
    try:
        field = frappe.get_meta(doctype).get_field(key) if doctype else None
        if field and field.fieldtype == "Select" and field.options:
            raw = [o.strip() for o in field.options.split("\n") if o.strip()]
    except Exception:
        raw = []
    if not raw:
        raw = sorted({str(r.get(key)) for r in rows if r.get(key) not in (None, "")})
    return raw


def _filters(columns, rows, doctype):
    """Filter definitions for the compact filter bar: status/currency selects and date ranges."""
    out = []
    for col in columns:
        key = col["key"]
        if key.startswith("_"):
            continue
        if col["type"] == "status":
            options = [{"v": v, "d": status_display(v)[0]} for v in _select_options(doctype, key, rows)]
            if options:
                out.append({"key": key, "label": col["label"], "type": "select", "options": options})
        elif col["type"] == "badge":
            options = [{"v": v, "d": v} for v in sorted({str(r.get(key)) for r in rows if r.get(key) not in (None, "")})]
            if len(options) > 1:
                out.append({"key": key, "label": col["label"], "type": "select", "options": options})
        elif col["type"] == "date" and not any(f["type"] == "date_range" for f in out):
            out.append({"key": key, "label": col["label"], "type": "date_range"})
    return out[:3]


def _header_actions(doctype, view):
    """The one primary action beside the title (create), only when the user may create the document."""
    label = view.get("new_label")
    if not (doctype and label):
        return []
    try:
        if not frappe.has_permission(doctype, "create"):
            return []
    except Exception:
        return []
    return [schema.action("new", label, kind="link", style="primary", icon="plus", href=frappe.utils.get_url(f"/app/{slug(doctype)}/new"), placement="header")]


def _safe_spec(spec):
    return {k: v for k, v in spec.items() if k in ("doctype", "fields", "filters", "group_by", "order_by", "limit", "offset")}


def _empty(doctype):
    plural = DOCTYPE_PLURALS.get(doctype) or "السجلات"
    return {"title": f"لا توجد {plural} مطابقة", "hint": "جرّب توسيع الفلاتر أو المدة الزمنية."}


def _is_ranking(columns, rows, spec):
    if not (spec.get("group_by") and 2 <= len(rows) <= 50):
        return False
    money = [c for c in columns if c["type"] in ("currency", "number")]
    text = [c for c in columns if c["type"] == "text"]
    return len(text) == 1 and len(money) >= 1


def _add_ranking(columns, rows):
    value_col = next(c for c in columns if c["type"] in ("currency", "number"))
    total = sum(fmt.to_number(r.get(value_col["key"])) or 0 for r in rows)
    ranked = sorted(rows, key=lambda r: fmt.to_number(r.get(value_col["key"])) or 0, reverse=True)
    for r in ranked:
        value = fmt.to_number(r.get(value_col["key"])) or 0
        r["_share"] = (value / total) if total else 0
    rank_col = schema.column("_rank", "#", "number", sortable=False, width="44px")
    share_col = schema.column("_share", "النسبة", "percentage")
    value_col["bar"] = True
    return [rank_col] + columns + [share_col], ranked


@register("data.query")
def present_query(tool, arguments, result, ctx):
    spec = _spec(arguments)
    doctype = spec.get("doctype")
    rows = result.get("rows") or []
    total = result.get("row_count", len(rows))
    if spec.get("group_by") or not isinstance(total, int):
        total = len(rows) if spec.get("group_by") else len(rows)
    presentation, _, _ = build_rows_view(rows, doctype=doctype, spec=spec, total_count=total)
    page_size = spec.get("limit") or 500
    paging = presentation["paging"]
    paging.update({"page_size": page_size, "page": (paging["offset"] // page_size) + 1,
                   "total_pages": max(1, -(-total // page_size))})
    presentation["paging"]["pageable"] = bool(doctype and not spec.get("group_by"))
    text = f"{fmt.number(total)} {DOCTYPE_PLURALS.get(doctype, 'سجل')}"
    kpis = presentation["kpis"]
    if kpis:
        text += "، " + "، ".join(f"{k['label']}: {k['d']}" for k in kpis)
    for w in presentation["warnings"]:
        text += f" ({w['text']})"
    summary = {"text": text, "records": total, "shown": len(rows),
               **{k["key"]: k["v"] for k in kpis}}
    return schema.envelope(
        tool=tool, data={"doctype": doctype, "row_count": total, "rows": rows[:25],
                         "rows_omitted": max(0, len(rows) - 25)},
        summary=summary, presentation=presentation,
        warnings=presentation["warnings"],
        meta={"doctype": doctype, "truncated": bool(result.get("truncated"))},
    )


@register("artifacts.create_table")
def present_artifact_table(tool, arguments, result, ctx):
    rows = arguments.get("rows") or []
    presentation, _, _ = build_rows_view(rows, title=arguments.get("title"), total_count=len(rows))
    return schema.envelope(tool=tool, data={"artifact": result.get("artifact_name"), "row_count": len(rows)},
                           summary={"text": f"{fmt.number(len(rows))} صف — {arguments.get('title') or 'جدول'}"},
                           presentation=presentation, warnings=presentation["warnings"])
