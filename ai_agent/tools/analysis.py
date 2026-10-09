"""analysis.sales: a period's sales, invoice/customer counts and the top customers, read with the caller's permissions."""

import frappe
from frappe.utils import flt

from ai_agent.metrics import periods

MAX_INVOICES = 20000
LABELS = {"today": "اليوم", "yesterday": "أمس", "this_week": "هذا الأسبوع", "last_week": "الأسبوع الماضي", "this_month": "هذا الشهر", "last_month": "الشهر الماضي",
          "this_quarter": "هذا الربع", "last_quarter": "الربع الماضي", "this_fiscal_year": "هذه السنة المالية", "custom": "الفترة المحددة"}


def handle_sales(arguments, permission_context):
    """Handle analysis.sales: totals plus top customers for one period and one company currency."""
    period = arguments.get("period") or "this_month"
    top = max(1, min(int(arguments.get("top") or 5), 20))
    company = arguments.get("company") or getattr(permission_context, "company", None)
    if not permission_context.has_doctype_permission("Sales Invoice", "read"):
        return {"error": "No read permission on 'Sales Invoice'"}
    try:
        info = periods.resolve_period(period, company)
    except periods.PeriodError as exc:
        return {"error": str(exc)}

    filters = [["docstatus", "=", 1], ["is_return", "=", 0], ["posting_date", "between", [str(info["from"]), str(info["to"])]]]
    if company:
        filters.append(["company", "=", company])
    rows = frappe.get_list("Sales Invoice", filters=filters, fields=["name", "customer", "customer_name", "base_grand_total", "company"],
                           user=permission_context.user, limit_page_length=MAX_INVOICES)
    companies = sorted({r.company for r in rows if r.company})
    currencies = {frappe.get_cached_value("Company", c, "default_currency") for c in companies}
    if len(currencies) > 1:
        return {"error": "The invoices span companies with different currencies; pass `company` so amounts are not added across currencies."}
    currency = next(iter(currencies), None)

    by_customer = {}
    for r in rows:
        entry = by_customer.setdefault(r.customer, {"customer": r.customer, "customer_name": r.customer_name or r.customer, "sales": 0.0, "invoices": 0})
        entry["sales"] += flt(r.base_grand_total)
        entry["invoices"] += 1
    total = sum(e["sales"] for e in by_customer.values())
    ranked = sorted(by_customer.values(), key=lambda e: -e["sales"])
    for e in ranked:
        e["share"] = (e["sales"] / total) if total else 0
    count = len(rows)
    return {
        "period": {"key": info["key"], "from": str(info["from"]), "to": str(info["to"]), "label": LABELS.get(info["key"], info["key"])},
        "company": company, "currency": currency, "truncated": count >= MAX_INVOICES,
        "totals": {"sales": total, "invoices": count, "customers": len(by_customer), "avg_invoice": (total / count) if count else 0},
        "top": ranked[:top],
        "top3_share": sum(e["share"] for e in ranked[:3]) if len(ranked) >= 3 else None,
    }
