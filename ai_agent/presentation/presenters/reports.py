"""reports.run: keep the report's own business column order, apply typed formatting and the right table variant."""

import frappe

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import insights as findings
from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t
from ai_agent.presentation.labels import FIELD_LABELS, doctype_label, label_for, translate
from ai_agent.presentation.presenters.lists import CURRENCY_KEYS, _default_currency
from ai_agent.presentation.registry import register
from ai_agent.presentation.urls import doc_href

FIELDTYPE = {
    "Currency": "currency", "Float": "number", "Int": "number", "Percent": "percentage",
    "Date": "date", "Datetime": "date", "Check": "boolean", "Link": "text", "Data": "text",
}
SPECIAL_ACCOUNTS = {"'opening'": "opening", "'total'": "total", "'closing (opening + total)'": "closing"}
LATE_BUCKETS = ("range3", "range4", "range5", "61-90", "91-120", "120-above")
REPORT_TITLES = {   # English titles, translated where shown
    "general ledger": "General Ledger", "accounts receivable": "Receivables Aging", "accounts payable": "Payables Aging",
    "accounts receivable summary": "Receivables Summary", "accounts payable summary": "Payables Summary",
    "stock balance": "Stock Balances", "stock ledger": "Stock Ledger", "trial balance": "Trial Balance",
    "balance sheet": "Balance Sheet", "profit and loss statement": "Income Statement", "sales register": "Sales Register",
    "purchase register": "Purchase Register", "customer ledger summary": "Customer Statements",
}
AGING_BUCKETS = ("range1", "range2", "range3", "range4", "range5", "0-30", "31-60", "61-90", "91-120", "120-above", "current")


def _variant(report_name, columns):
    name = (report_name or "").lower()
    keys = {c["fieldname"] for c in columns}
    if "trial balance" in name or "balance sheet" in name or "profit and loss" in name or {"closing_debit", "closing_credit"} <= keys:
        return "financial"          # one row per account: a statement of balances, not a ledger of entries
    if "general ledger" in name or {"debit", "credit"} <= keys:
        return "statement" if "balance" in keys else "gl"
    if "receivable" in name or "payable" in name or "aging" in name or keys & set(AGING_BUCKETS):
        return "aging"
    if "stock" in name:
        return "stock"
    return "financial" if any(c.get("fieldtype") == "Currency" for c in columns) else "data"


@register("reports.run")
def present_report(tool, arguments, result, ctx):
    rows = [r for r in (result.get("result") or []) if not isinstance(r, dict) or any(v not in (None, "") for v in r.values())]
    if result.get("is_pending"):
        return None
    report_columns = [c for c in (result.get("columns") or []) if c.get("fieldname")]
    if not rows or not report_columns or not isinstance(rows[0], dict):
        return None
    currency = _default_currency({})
    variant = _variant(result.get("report"), report_columns)
    keys = [c["fieldname"] for c in report_columns]
    currency_key = next((k for k in CURRENCY_KEYS if k in keys), None)
    mixed = currency_key and len({r.get(currency_key) for r in rows if r.get(currency_key)}) > 1

    columns = []
    for rc in report_columns:
        key = rc["fieldname"]
        if key in CURRENCY_KEYS and not mixed:
            continue
        kind = FIELDTYPE.get(rc.get("fieldtype"), None) or schema.infer_column(key, [r.get(key) for r in rows])["type"]
        # A known business field keeps its Arabic name; report-specific columns keep the report's label.
        col = schema.column(key, label_for(key) if key in FIELD_LABELS else translate(rc.get("label") or "") or label_for(key), kind)
        if rc.get("fieldtype") == "Percent":
            col["percent_value"] = True
        if key in ("voucher_no", "name", "party", "account"):
            col["priority"] = "primary"
        if kind == "currency" and key not in ("balance", "outstanding") and variant != "statement":
            col["total"] = not mixed
        if kind == "currency" and key in ("debit", "credit", "outstanding", "total"):
            col["total"] = not mixed
        columns.append(col)
    # Report-declared columns whose label is a raw fieldname get the business label.
    for col in columns:
        if col["label"] == col["key"]:
            col["label"] = label_for(col["key"])
    if columns:
        if not any(c["priority"] == "primary" for c in columns):
            columns[0]["priority"] = "primary"
        for col in columns[1:3]:
            if col["priority"] == "supporting":
                col["priority"] = "secondary"

    for col in columns[7:]:
        if col["priority"] != "primary":
            col["collapsed"] = True
    if variant in ("statement", "gl"):
        # The voucher type is already part of the document number and the remark; it is not shown but drives the tabs.
        for col in columns:
            if col["key"] == "voucher_type":
                col["hidden"] = True
    out_rows, data_rows, special = [], [], {}
    for r in rows:
        kind = SPECIAL_ACCOUNTS.get(str(r.get("account") or "").strip().lower())
        if kind:
            special[kind] = r
        else:
            data_rows.append(r)
    group_by = None
    if variant == "gl" and "account" in {c["key"] for c in columns}:
        accounts = {str(r.get("account")) for r in data_rows if r.get("account")}
        if 2 <= len(accounts) <= 30 and len(accounts) < len(data_rows):      # grouping needs several entries per account
            group_by = "account"
            data_rows.sort(key=lambda r: (str(r.get("account") or ""), str(r.get("posting_date") or "")))
    rows_all = ([special[k] for k in ("opening",) if k in special] + data_rows + [special[k] for k in ("total", "closing") if k in special]) if group_by else rows
    rows = data_rows
    for r in rows_all:
        kind = SPECIAL_ACCOUNTS.get(str(r.get("account") or "").strip().lower())
        if kind == "total":
            continue  # the totals row is rebuilt from the data rows, never listed twice
        cells = {c["key"]: schema.cell(r.get(c["key"]), c, currency=(r.get(currency_key) if mixed else currency)) for c in columns}
        for c in columns:
            if c.get("percent_value") and cells[c["key"]]["v"] is not None:
                cells[c["key"]]["d"] = fmt.percent_value(cells[c["key"]]["v"])
        vt, vn = r.get("voucher_type"), r.get("voucher_no")
        actions = _row_actions(vt, vn)
        if not actions and r.get("account") and not kind and variant not in ("statement", "gl"):
            ledger = findings.ledger_drill(result.get("filters"), account=r.get("account"))
            if ledger:
                actions = [schema.action("ledger", t("Ledger entries"), kind="drill", style="primary", tool="reports__run", arguments=ledger)]
        row = schema.make_row(cells, id=str(vn or r.get("account") or "") or None, href=doc_href(vt, vn) if vt and vn else None,
                              actions=actions, kind=kind if kind in ("opening", "closing") else None,
                              level=frappe.utils.cint(r.get("indent")) if r.get("indent") else 0)
        if variant == "aging":
            for c in columns:
                if c["key"] in LATE_BUCKETS and (fmt.to_number(r.get(c["key"])) or 0) > 0:
                    cells[c["key"]]["tone"] = "danger" if c["key"] in LATE_BUCKETS[-3:] or c["key"] in ("91-120", "120-above") else "warning"
        if "voucher_type" in cells and r.get("voucher_type"):
            cells["voucher_type"]["d"] = doctype_label(r["voucher_type"])   # the business name, not the DocType
        if kind in ("opening", "closing"):
            for key, cell in cells.items():
                if cell["d"] in ("—", "0 YER", "0") and key != "balance":
                    cell["d"] = ""
        out_rows.append(row)

    for row, r in zip([x for x in out_rows], [x for x in rows_all if SPECIAL_ACCOUNTS.get(str(x.get("account") or "").strip().lower()) != "total"]):
        kind = SPECIAL_ACCOUNTS.get(str(r.get("account") or "").strip().lower())
        if kind:
            # The label goes in the account column, or the first column when the report has none.
            target = "account" if "account" in row["cells"] else columns[0]["key"]
            row["cells"][target]["d"] = t({"opening": "Opening balance", "closing": "Closing balance"}[kind])

    sums = {c["key"]: sum(fmt.to_number(r.get(c["key"])) or 0 for r in rows) for c in columns if c.get("total")}
    totals = {"label": t("Total"), "cells": {k: {"v": v, "d": fmt.money(v, currency)} for k, v in sums.items()}} if sums and not mixed else None
    kpis = []
    labels = {c["key"]: c["label"] for c in columns}
    statement = variant in ("statement", "gl") and "debit" in sums and "credit" in sums
    for key in ("debit", "credit", "outstanding", "total"):
        if key in sums:
            if statement and key == "debit":
                kpis.append(schema.kpi(key, t("Total debit"), sums[key], currency=currency, icon="coins", accent="purple"))
            elif statement and key == "credit":
                kpis.append(schema.kpi(key, t("Total credit"), sums[key], currency=currency, icon="banknote", tone="success"))
            else:
                kpis.append(schema.kpi(key, labels[key], sums[key], currency=currency))
    warnings, actions, insights = [], [], []
    party = (result.get("filters") or {}).get("party")
    party = party[0] if isinstance(party, list) and party else party
    overdue = _overdue(result.get("filters") or {}, party, ctx) if variant == "statement" and party else None
    if overdue:
        overdue_money = fmt.money(overdue["amount"], overdue["currency"] or currency)
        several = overdue["count"] > 1
        kpis.append(schema.kpi("overdue", t("Overdue amount"), overdue["amount"], currency=overdue["currency"] or currency, icon="clockAlert", tone="danger",
                               hint=t("Invoices: {0}", fmt.number(overdue["count"])) if several else t("One invoice")))
        warnings.append(schema.warning("critical", t("Alert: {0} overdue invoices totaling {1}.", fmt.number(overdue["count"]), overdue_money) if several
                                       else t("Alert: one overdue invoice of {0}.", overdue_money), "overdue"))
        actions.append(schema.action("overdue", t("Show overdue invoices"), kind="drill", style="soft", icon="receipt", tool="data__query", arguments=overdue["query"]))
    if statement:
        closing = special.get("closing", {})
        balance = closing.get("balance") if closing.get("balance") is not None else sums["debit"] - sums["credit"]
        kpis.append(schema.kpi("balance", t("Current balance") if closing else t("Balance"), balance, currency=currency, icon="wallet",
                               tone="info"))
        if party and _can("Payment Entry", "create"):
            actions.append(schema.action("pay", t("Create payment"), kind="link", style="soft", icon="card",
                                         href=frappe.utils.get_url(f"/app/payment-entry/new?party_type={'Supplier' if (result.get('filters') or {}).get('party_type') == 'Supplier' else 'Customer'}&party={frappe.utils.quote(str(party))}")))
        actions.append(schema.action("export", t("Export"), kind="export", style="", icon="download"))
    name_lower = str(result.get("report") or "").lower()
    if not mixed:
        if "trial balance" in name_lower:
            insights += findings.abnormal_balances(rows, result.get("filters"), currency)
        elif variant == "aging":
            insights += findings.opposite_party_balances(rows, result.get("filters"), currency, payable="payable" in name_lower)
        elif variant in ("statement", "gl"):
            insights += findings.dominant_voucher(rows, currency, label=str(party) if party else None)
    if mixed:
        warnings.append(schema.warning("info", t("This report has several currencies, so totals are not added across currencies."), "mixed_currency"))
    reported = result.get("row_count", len(rows_all))
    total_rows = max(len(rows), reported - (len(rows_all) - len(rows))) if reported else len(rows)
    if result.get("is_truncated") or total_rows > len(rows):
        warnings.append(schema.warning("info", t("Showing {0} of {1} rows.", fmt.number(len(rows)), fmt.number(total_rows)), "truncated"))
    report_name = result.get("report") or t("Report")
    title = t(REPORT_TITLES[str(report_name).lower()]) if str(report_name).lower() in REPORT_TITLES else translate(report_name) or report_name
    subtitle = None
    if variant == "statement" and party:
        title = t("Statement of account — {0}", party)
        subtitle = t("As of {0}", fmt.date((result.get("filters") or {}).get("to_date") or frappe.utils.nowdate()))
    date_col = next((c["key"] for c in columns if c["type"] == "date"), None)
    voucher_col = "voucher_type" in {c["key"] for c in columns}
    tabs = [dict(tab, label=t(tab["label"])) for tab in TABS] if variant in ("statement", "gl") and voucher_col else []
    presentation = schema.table(
        title, columns, out_rows, variant=variant, subtitle=subtitle, summary={"records": total_rows}, kpis=kpis[:4], totals=totals,
        warnings=warnings, currency=currency, sort=None, group_by=group_by, actions=actions, insights=insights,
        icon="user" if variant == "statement" else "file", tabs=tabs,
        filters=[{"key": date_col, "label": next(c["label"] for c in columns if c["key"] == date_col), "type": "date_range"}] if date_col else [],
        empty={"title": t("No data for these filters"), "hint": t("Change the period or the company.")},
        paging={"total": total_rows, "shown": len(rows), "offset": 0, "noun": t("entries") if statement else t("rows")},
        developer={"report": report_name, "filters": result.get("filters")})
    text = t("{0}: {1} rows", title, fmt.number(total_rows)) + "".join(f"{fmt.comma()}{k['label']} {k['d']}" for k in kpis[:4])
    if insights:
        text += " " + t("Notes:") + " " + " | ".join(schema.insight_text(i) for i in insights)
    return schema.envelope(tool=tool, data={"report": report_name, "row_count": total_rows, "summary": result.get("summary"), "filters": result.get("filters")},
                           summary={"text": text, "records": total_rows, **{k["key"]: k["v"] for k in kpis}},
                           presentation=presentation, warnings=warnings)


TABS = [
    {"key": "all", "label": "All"},             # English labels, translated in present_report
    {"key": "invoices", "label": "Invoices", "col": "voucher_type", "in": ["Sales Invoice", "Purchase Invoice"]},
    {"key": "payments", "label": "Payments", "col": "voucher_type", "in": ["Payment Entry"]},
    {"key": "entries", "label": "Journal entries", "col": "voucher_type", "in": ["Journal Entry"]},
]


def _can(doctype, ptype):
    try:
        return bool(frappe.has_permission(doctype, ptype))
    except Exception:
        return False


def _row_actions(voucher_type, voucher_no):
    if not (voucher_type and voucher_no):
        return None
    return [schema.action("open", t("View"), kind="drill", style="primary", overflow=True, tool="data__get_document",
                          arguments={"doctype": voucher_type, "name": voucher_no}),
            schema.action("erp", t("Open in ERP"), kind="link", href=doc_href(voucher_type, voucher_no), overflow=True),
            schema.action("copy", t("Copy number"), kind="copy", arguments={"text": str(voucher_no)}, overflow=True)]


def _overdue(filters, party, ctx):
    """Overdue invoices of the party, read with the caller's permissions. None when there are none."""
    supplier = filters.get("party_type") == "Supplier"
    doctype, field = ("Purchase Invoice", "supplier") if supplier else ("Sales Invoice", "customer")
    today = frappe.utils.nowdate()
    rules = [{"field": field, "operator": "=", "value": party}, {"field": "docstatus", "operator": "=", "value": 1},
             {"field": "outstanding_amount", "operator": ">", "value": 0}, {"field": "due_date", "operator": "<", "value": today}]
    try:
        rows = frappe.get_list(doctype, filters=[[doctype, r["field"], r["operator"], r["value"]] for r in rules],
                               fields=["name", "outstanding_amount", "currency"], user=getattr(ctx, "user", None) or frappe.session.user, limit_page_length=0)
    except Exception:
        return None
    if not rows:
        return None
    currencies = {r.currency for r in rows if r.currency}
    if len(currencies) > 1:
        return None  # never add amounts across currencies
    query = {"specification": {"doctype": doctype, "fields": ["name", field + "_name", "posting_date", "due_date", "grand_total", "outstanding_amount", "status", "currency"],
                               "filters": rules, "order_by": [{"field": "due_date", "direction": "asc"}], "limit": 100}}
    return {"count": len(rows), "amount": sum(r.outstanding_amount or 0 for r in rows), "currency": next(iter(currencies), None), "query": query}
