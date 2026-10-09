"""Stock balance and invoice payments views."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.presenters.lists import _default_currency
from ai_agent.presentation.registry import register
from ai_agent.presentation.urls import doc_href


@register("erp.stock_balance")
def present_stock_balance(tool, arguments, result, ctx):
    currency = _default_currency({})
    qty = fmt.to_number(result.get("qty")) or 0
    warnings = []
    if qty <= 0:
        warnings.append(schema.warning("critical" if qty < 0 else "warning",
                                       "لا يوجد رصيد متاح لهذا الصنف في المخزن." if qty == 0 else "الرصيد سالب — راجع حركات المخزون.", "low_stock"))
    columns = [
        schema.column("item_code", "الصنف", priority="primary"),
        schema.column("warehouse", "المخزن", priority="secondary"),
        schema.column("qty", "الكمية", "number", total=True),
        schema.column("valuation_rate", "سعر التقييم", "currency"),
        schema.column("stock_value", "قيمة المخزون", "currency", total=True),
    ]
    row = {"item_code": result.get("item_code"), "warehouse": result.get("warehouse"), "qty": qty,
           "valuation_rate": result.get("valuation_rate"), "stock_value": result.get("stock_value")}
    cells = {c["key"]: schema.cell(row[c["key"]], c, currency=currency) for c in columns}
    presentation = schema.table(
        "رصيد المخزون", columns, [schema.make_row(cells, id=row["item_code"], href=doc_href("Item", row["item_code"]))],
        variant="stock", subtitle=f"حتى {fmt.date(result.get('date'))}",
        kpis=[schema.kpi("qty", "الكمية المتاحة", qty, type="number", tone="danger" if qty < 0 else "warning" if qty == 0 else "success"),
              schema.kpi("stock_value", "قيمة المخزون", result.get("stock_value"), currency=currency)],
        warnings=warnings, currency=currency, paging={"total": 1, "shown": 1, "offset": 0})
    return schema.envelope(tool=tool, data=result,
                           summary={"text": f"{row['item_code']} في {row['warehouse']}: {fmt.number(qty)} وحدة، القيمة {fmt.money(result.get('stock_value'), currency)}"},
                           presentation=presentation, warnings=warnings)


@register("erp.invoice_payments")
def present_invoice_payments(tool, arguments, result, ctx):
    payments = result.get("payments") or []
    currency = _default_currency({})
    columns = [
        schema.column("posting_date", "التاريخ", "date", priority="secondary"),
        schema.column("voucher_no", "السند", priority="primary"),
        schema.column("voucher_type", "النوع", "badge"),
        schema.column("amount", "المبلغ", "currency", total=True),
    ]
    rows = []
    for p in payments:
        cells = {c["key"]: schema.cell(p.get(c["key"]), c, currency=p.get("account_currency") or currency) for c in columns}
        rows.append(schema.make_row(cells, id=p.get("voucher_no"), href=doc_href(p.get("voucher_type"), p.get("voucher_no"))))
    outstanding = result.get("outstanding_amount")
    kpis = [schema.kpi("grand_total", "إجمالي الفاتورة", result.get("grand_total"), currency=currency),
            schema.kpi("outstanding_amount", "المستحق", outstanding, currency=currency,
                       tone="warning" if (fmt.to_number(outstanding) or 0) > 0 else "success")]
    total = sum(fmt.to_number(p.get("amount")) or 0 for p in payments)
    presentation = schema.table(
        f"مدفوعات الفاتورة {result.get('invoice')}", columns, rows, variant="financial", kpis=kpis,
        totals={"label": "إجمالي المدفوع", "cells": {"amount": {"v": total, "d": fmt.money(total, currency)}}} if rows else None,
        empty={"title": "لا توجد مدفوعات على هذه الفاتورة", "hint": "لم يُسجَّل أي سند دفع مرتبط بها."},
        paging={"total": len(rows), "shown": len(rows), "offset": 0}, currency=currency)
    return schema.envelope(tool=tool, data=result,
                           summary={"text": f"{len(rows)} دفعة على {result.get('invoice')}، المستحق {fmt.money(outstanding, currency)}"},
                           presentation=presentation)
