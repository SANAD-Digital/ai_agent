"""analysis.sales: KPI cards, a top-customers chart, a ranking table and one computed insight."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t
from ai_agent.presentation.presenters.metrics import chart_spec
from ai_agent.presentation.registry import register
from ai_agent.presentation.urls import doc_href


@register("analysis.sales")
def present_sales(tool, arguments, result, ctx):
    currency, totals, top, period = result.get("currency"), result["totals"], result.get("top") or [], result["period"]
    title = t("Sales — {0}", _period_title(period))
    kpis = [
        schema.kpi("sales", t("Total sales"), totals["sales"], currency=currency, icon="chart", tone="info", compact=True),
        schema.kpi("customers", t("Customer count"), totals["customers"], type="number", icon="users", tone="success"),
        schema.kpi("invoices", t("Invoice count"), totals["invoices"], type="number", icon="file", accent="purple"),
        schema.kpi("avg", t("Average invoice"), totals["avg_invoice"], currency=currency, icon="layers", tone="warning", compact=True),
    ]
    columns = [
        schema.column("_rank", "#", "number", sortable=False, width="44px"),
        schema.column("customer_name", t("Customer"), priority="primary"),
        schema.column("sales", t("Sales"), "currency", priority="secondary", total=True),
        schema.column("share", t("Contribution"), "percentage"),
        schema.column("invoices", t("Invoices"), "number"),
    ]
    columns[2]["bar"] = True
    rows = []
    for i, e in enumerate(top, start=1):
        cells = {"_rank": {"v": i, "d": str(i)}}
        for c in columns[1:]:
            cells[c["key"]] = schema.cell(e.get(c["key"]), c, currency=currency)
        rows.append(schema.make_row(cells, id=str(e["customer"]), href=doc_href("Customer", e["customer"]), actions=[
            schema.action("open", t("View"), kind="drill", style="primary", overflow=True, tool="data__get_document", arguments={"doctype": "Customer", "name": e["customer"]}),
            schema.action("erp", t("Open in ERP"), kind="link", href=doc_href("Customer", e["customer"]), overflow=True)]))
    chart = None
    if len(top) >= 2 and any(e["sales"] for e in top):
        chart = chart_spec("bar", [e["customer_name"] for e in top], [{"name": t("Sales"), "values": [e["sales"] for e in top]}], unit=currency,
                           title=t("Top {0} customers by sales", fmt.number(len(top))))
    insights = []
    if result.get("top3_share") is not None:
        share = result["top3_share"]
        insights.append(schema.insight(
            t("Sales concentration"), t("The top 3 customers make up {0} of the period's sales.", fmt.percent(share, 0)),
            cause=t("Sales depend on a small number of customers.") if share >= 0.5 else None,
            impact=t("Losing one of them directly affects the period's revenue.") if share >= 0.5 else None,
            action=t("Keep an eye on these customers' balances and credit limits.") if share >= 0.5 else None,
            level="warning" if share >= 0.7 else "info"))
    warnings = [schema.warning("info", t("Only the first {0} invoices were analyzed; narrow the period for a complete figure.", fmt.number(20000)), "truncated")] if result.get("truncated") else []
    presentation = schema.table(
        title, columns, rows, variant="ranking", subtitle=t("Customer performance analysis"), kpis=kpis, chart=chart, warnings=warnings, insights=insights,
        icon="chart", currency=currency, empty={"title": t("No sales in this period"), "hint": t("Try another period or company.")},
        paging={"total": len(rows), "shown": len(rows), "offset": 0, "noun": t("customers")}, sort=None,
        developer={"period": period, "company": result.get("company")})
    presentation["table_title"] = t("Customer ranking")
    text = (t("{0}: sales {1}, {2} invoices, {3} customers", title, fmt.money(totals["sales"], currency),
              fmt.number(totals["invoices"]), fmt.number(totals["customers"]))
            + (fmt.comma() + schema.insight_text(insights[0]) if insights else ""))
    return schema.envelope(tool=tool, data={"period": period, "currency": currency, "totals": totals, "top": top}, summary={"text": text, **totals},
                           presentation=presentation, warnings=warnings)


def _period_title(period):
    if period["key"] in ("this_month", "last_month"):
        return fmt.date(period["from"]).split(" ", 1)[1]   # "September 2026"
    return f"{fmt.date(period['from'])} → {fmt.date(period['to'])}"
