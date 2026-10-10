"""Stock balance and invoice payments views."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t
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
                                       t("No stock available for this item in this warehouse.") if qty == 0
                                       else t("The balance is negative. Review the stock movements."), "low_stock"))
    columns = [
        schema.column("item_code", t("Item"), priority="primary"),
        schema.column("warehouse", t("Warehouse"), priority="secondary"),
        schema.column("qty", t("Quantity"), "number", total=True),
        schema.column("valuation_rate", t("Valuation rate"), "currency"),
        schema.column("stock_value", t("Stock value"), "currency", total=True),
    ]
    row = {"item_code": result.get("item_code"), "warehouse": result.get("warehouse"), "qty": qty,
           "valuation_rate": result.get("valuation_rate"), "stock_value": result.get("stock_value")}
    cells = {c["key"]: schema.cell(row[c["key"]], c, currency=currency) for c in columns}
    presentation = schema.table(
        t("Stock balance"), columns, [schema.make_row(cells, id=row["item_code"], href=doc_href("Item", row["item_code"]))],
        variant="stock", subtitle=t("As of {0}", fmt.date(result.get("date"))),
        kpis=[schema.kpi("qty", t("Available quantity"), qty, type="number", tone="danger" if qty < 0 else "warning" if qty == 0 else "success"),
              schema.kpi("stock_value", t("Stock value"), result.get("stock_value"), currency=currency)],
        warnings=warnings, currency=currency, paging={"total": 1, "shown": 1, "offset": 0})
    return schema.envelope(tool=tool, data=result,
                           summary={"text": t("{0} in {1}: {2} units, value {3}", row["item_code"], row["warehouse"], fmt.number(qty),
                                                fmt.money(result.get("stock_value"), currency))},
                           presentation=presentation, warnings=warnings)


@register("erp.invoice_payments")
def present_invoice_payments(tool, arguments, result, ctx):
    payments = result.get("payments") or []
    currency = _default_currency({})
    columns = [
        schema.column("posting_date", t("Date"), "date", priority="secondary"),
        schema.column("voucher_no", t("Payment voucher"), priority="primary"),
        schema.column("voucher_type", t("Type"), "badge"),
        schema.column("amount", t("Amount"), "currency", total=True),
    ]
    rows = []
    for p in payments:
        cells = {c["key"]: schema.cell(p.get(c["key"]), c, currency=p.get("account_currency") or currency) for c in columns}
        rows.append(schema.make_row(cells, id=p.get("voucher_no"), href=doc_href(p.get("voucher_type"), p.get("voucher_no"))))
    outstanding = result.get("outstanding_amount")
    kpis = [schema.kpi("grand_total", t("Invoice total"), result.get("grand_total"), currency=currency),
            schema.kpi("outstanding_amount", t("Outstanding"), outstanding, currency=currency,
                       tone="warning" if (fmt.to_number(outstanding) or 0) > 0 else "success")]
    total = sum(fmt.to_number(p.get("amount")) or 0 for p in payments)
    presentation = schema.table(
        t("Payments on invoice {0}", result.get("invoice")), columns, rows, variant="financial", kpis=kpis,
        totals={"label": t("Total paid"), "cells": {"amount": {"v": total, "d": fmt.money(total, currency)}}} if rows else None,
        empty={"title": t("No payments on this invoice"), "hint": t("No payment voucher is linked to it.")},
        paging={"total": len(rows), "shown": len(rows), "offset": 0}, currency=currency)
    return schema.envelope(tool=tool, data=result,
                           summary={"text": t("{0} payments on {1}, outstanding {2}", len(rows), result.get("invoice"), fmt.money(outstanding, currency))},
                           presentation=presentation)
