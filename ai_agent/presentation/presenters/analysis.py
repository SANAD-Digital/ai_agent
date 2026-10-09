"""analysis.sales: KPI cards, a top-customers chart, a ranking table and one computed insight."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.presenters.metrics import chart_spec
from ai_agent.presentation.registry import register
from ai_agent.presentation.urls import doc_href


@register("analysis.sales")
def present_sales(tool, arguments, result, ctx):
    currency, totals, top, period = result.get("currency"), result["totals"], result.get("top") or [], result["period"]
    title = f"المبيعات — {_period_title(period)}"
    kpis = [
        schema.kpi("sales", "إجمالي المبيعات", totals["sales"], currency=currency, icon="chart", tone="info", compact=True),
        schema.kpi("customers", "عدد العملاء", totals["customers"], type="number", icon="users", tone="success"),
        schema.kpi("invoices", "عدد الفواتير", totals["invoices"], type="number", icon="file", accent="purple"),
        schema.kpi("avg", "متوسط الفاتورة", totals["avg_invoice"], currency=currency, icon="layers", tone="warning", compact=True),
    ]
    columns = [
        schema.column("_rank", "#", "number", sortable=False, width="44px"),
        schema.column("customer_name", "العميل", priority="primary"),
        schema.column("sales", "المبيعات", "currency", priority="secondary", total=True),
        schema.column("share", "المساهمة", "percentage"),
        schema.column("invoices", "الفواتير", "number"),
    ]
    columns[2]["bar"] = True
    rows = []
    for i, e in enumerate(top, start=1):
        cells = {"_rank": {"v": i, "d": str(i)}}
        for c in columns[1:]:
            cells[c["key"]] = schema.cell(e.get(c["key"]), c, currency=currency)
        rows.append(schema.make_row(cells, id=str(e["customer"]), href=doc_href("Customer", e["customer"]), actions=[
            schema.action("open", "فتح", kind="drill", style="primary", overflow=True, tool="data__get_document", arguments={"doctype": "Customer", "name": e["customer"]}),
            schema.action("erp", "فتح في ERP", kind="link", href=doc_href("Customer", e["customer"]), overflow=True)]))
    chart = None
    if len(top) >= 2 and any(e["sales"] for e in top):
        chart = chart_spec("bar", [e["customer_name"] for e in top], [{"name": "المبيعات", "values": [e["sales"] for e in top]}], unit=currency,
                           title=f"أعلى {fmt.number(len(top))} عملاء من حيث المبيعات")
    insights = []
    if result.get("top3_share") is not None:
        share = result["top3_share"]
        insights.append(schema.insight(
            "تركّز المبيعات", f"أعلى 3 عملاء يمثلون {fmt.percent(share, 0)} من مبيعات الفترة.",
            cause="اعتماد المبيعات على عدد قليل من العملاء." if share >= 0.5 else None,
            impact="خسارة أحدهم تؤثر مباشرة على إيراد الفترة." if share >= 0.5 else None,
            action="تابع أرصدة هؤلاء العملاء وحدود ائتمانهم." if share >= 0.5 else None,
            level="warning" if share >= 0.7 else "info"))
    warnings = [schema.warning("info", f"حُللت أول {fmt.number(20000)} فاتورة فقط؛ ضيّق الفترة للحصول على رقم كامل.", "truncated")] if result.get("truncated") else []
    presentation = schema.table(
        title, columns, rows, variant="ranking", subtitle="تحليل أداء العملاء", kpis=kpis, chart=chart, warnings=warnings, insights=insights,
        icon="chart", currency=currency, empty={"title": "لا توجد مبيعات في هذه الفترة", "hint": "جرّب فترة أخرى أو شركة أخرى."},
        paging={"total": len(rows), "shown": len(rows), "offset": 0, "noun": "عملاء"}, sort=None,
        developer={"period": period, "company": result.get("company")})
    presentation["table_title"] = "ترتيب العملاء"
    text = (f"{title}: المبيعات {fmt.money(totals['sales'], currency)}، {fmt.number(totals['invoices'])} فاتورة، {fmt.number(totals['customers'])} عميل"
            + (f"، {schema.insight_text(insights[0])}" if insights else ""))
    return schema.envelope(tool=tool, data={"period": period, "currency": currency, "totals": totals, "top": top}, summary={"text": text, **totals},
                           presentation=presentation, warnings=warnings)


def _period_title(period):
    if period["key"] in ("this_month", "last_month"):
        return fmt.date(period["from"]).split(" ", 1)[1]   # "سبتمبر 2026"
    return f"{fmt.date(period['from'])} → {fmt.date(period['to'])}"
