"""metrics.query: KPI strip, chart and ranking / comparison table."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.labels import label_for
from ai_agent.presentation.presenters.lists import _default_currency
from ai_agent.presentation.registry import register

PERIOD_DIMENSIONS = ("month", "period", "date", "day", "week", "year", "quarter", "posting_date")
COUNT_HINTS = ("count", "عدد", "number")
DONUT_MAX = 6


def chart_spec(kind, labels, series, *, unit=None, title=None):
    """Chart only when it adds understanding; donut is limited to a few categories."""
    if kind == "donut" and len(labels) > DONUT_MAX:
        kind = "bar"
    return {"kind": kind, "labels": [str(l) for l in labels], "title": title, "unit": unit,
            "series": [{"name": s["name"], "values": [fmt.to_number(v) or 0 for v in s["values"]],
                        "d": [fmt.money(v, unit) if unit else fmt.number(v) for v in s["values"]]} for s in series]}


def _percent_ratio(change):
    percent = (change or {}).get("percent")
    return None if percent is None else percent / 100


@register("metrics.query")
def present_metric(tool, arguments, result, ctx):
    currency = _default_currency({})
    label = result.get("label") or result.get("metric")
    is_count = any(h in f"{result.get('metric', '')}{label or ''}".lower() for h in COUNT_HINTS)
    kind = "number" if is_count else "currency"
    period = (result.get("period") or {}).get("range")

    if "metrics" in result or "components" in result or ("value" in result and "rows" not in result):
        return _composite(tool, result, label, period, currency)

    dims = result.get("dimensions") or []
    rows = result.get("rows") or []
    compare = result.get("compare")
    total = result.get("total")
    delta = _percent_ratio((compare or {}).get("change"))
    warnings = [schema.warning("info", n, "note") for n in (result.get("notes") or [])]
    if result.get("pending_confirmation"):
        warnings.append(schema.warning("warning", "تعريف هذا المؤشر بانتظار تأكيد المحاسب.", "pending_metric"))
    kpis = [schema.kpi("total", label, total, type=kind, currency=currency, delta=delta,
                       hint=(f"مقارنة بـ {compare['range']}" if compare else None))]
    if compare:
        kpis.append(schema.kpi("previous", "الفترة السابقة", compare.get("total"), type=kind, currency=currency))

    title = label or "مؤشر"
    if not dims:
        presentation = {"type": "cards", "title": title, "subtitle": period, "kpis": kpis, "warnings": warnings,
                        "actions": [], "insights": [], "developer": {"source": result.get("source"), "assumption": result.get("assumption")}}
        text = f"{title} ({period}): {fmt.money(total, currency) if kind == 'currency' else fmt.number(total)}"
        return schema.envelope(tool=tool, data=result, summary={"text": text, "total": total}, presentation=presentation, warnings=warnings)

    dim = dims[0]
    columns = [schema.column("_rank", "#", "number", sortable=False, width="44px"),
               schema.column(dim, label_for(dim), priority="primary"),
               schema.column("value", title, kind, priority="secondary", total=True)]
    if compare:
        columns += [schema.column("previous", "السابق", kind), schema.column("change", "التغير", "percentage")]
    columns.append(schema.column("_share", "النسبة", "percentage"))
    columns[2]["bar"] = True
    is_time = dim in PERIOD_DIMENSIONS
    ordered = list(rows)
    if is_time:
        ordered.sort(key=lambda r: str(r.get(dim) or ""))
    out_rows = []
    grand = sum(fmt.to_number(r.get("value")) or 0 for r in rows)
    for i, r in enumerate(ordered):
        row = dict(r)
        row["_share"] = (fmt.to_number(r.get("value")) or 0) / grand if grand else 0
        row["change"] = _percent_ratio(r.get("change"))
        cells = {"_rank": {"v": i + 1, "d": str(i + 1)}}
        for c in columns[1:]:
            cells[c["key"]] = schema.cell(row.get(c["key"]), c, currency=currency)
        if compare and row["change"] is not None:
            cells["change"]["tone"] = "success" if row["change"] > 0 else "danger" if row["change"] < 0 else "neutral"
        out_rows.append(schema.make_row(cells, id=str(r.get(dim))))
    if len(out_rows) >= 3 and not is_time:
        top3 = sum(fmt.to_number(r.get("value")) or 0 for r in sorted(rows, key=lambda r: -(fmt.to_number(r.get("value")) or 0))[:3])
        kpis.append(schema.kpi("top3", "حصة أعلى 3", top3 / grand if grand else 0, type="percentage"))
    chart = None
    if len(rows) >= 2:
        chart_kind = "line" if is_time else "bar"
        chart = chart_spec(chart_kind, [r.get(dim) for r in ordered], [{"name": title, "values": [r.get("value") for r in ordered]}], unit=currency if kind == "currency" else None, title=title)
    presentation = schema.table(
        title, columns, out_rows, variant="comparison" if compare else "ranking",
        subtitle=period, summary={"records": len(rows)}, kpis=kpis, chart=chart, warnings=warnings, currency=currency,
        totals={"label": "الإجمالي", "cells": {"value": {"v": total, "d": fmt.money(total, currency) if kind == "currency" else fmt.number(total)}}},
        sort={"key": dim, "dir": "asc"} if is_time else {"key": "value", "dir": "desc"},
        empty={"title": "لا توجد بيانات لهذه الفترة", "hint": "جرّب فترة أخرى."},
        paging={"total": len(rows), "shown": len(rows), "offset": 0}, developer={"source": result.get("source"), "assumption": result.get("assumption")})
    text = f"{title} ({period}): {fmt.money(total, currency) if kind == 'currency' else fmt.number(total)} عبر {len(rows)} {label_for(dim)}"
    if delta is not None:
        text += f"، التغير {fmt.percent(delta)}"
    return schema.envelope(tool=tool, data=result, summary={"text": text, "total": total, "records": len(rows)},
                           presentation=presentation, warnings=warnings)


def _composite(tool, result, label, period, currency):
    parts = result.get("components") or result.get("metrics") or []
    kpis = []
    if "value" in result:
        kpis.append(schema.kpi("value", label, result["value"], currency=currency, delta=_percent_ratio(result.get("change"))))
    for p in parts:
        kpis.append(schema.kpi(p.get("metric"), p.get("label"), p.get("value"), currency=currency, delta=_percent_ratio(p.get("change"))))
    warnings = [schema.warning("warning", "تعريف هذا المؤشر بانتظار تأكيد المحاسب.", "pending_metric")] if result.get("pending_confirmation") else []
    presentation = {"type": "cards", "title": label, "subtitle": period, "kpis": kpis, "warnings": warnings,
                    "actions": [], "insights": [], "developer": {"assumption": result.get("assumption")}}
    text = f"{label} ({period}): " + "، ".join(f"{k['label']} {k['d']}" for k in kpis)
    return schema.envelope(tool=tool, data=result, summary={"text": text}, presentation=presentation, warnings=warnings)


CHART_KINDS = {"bar": "bar", "line": "line", "pie": "donut", "donut": "donut", "percentage": "donut"}


@register("artifacts.create_chart")
def present_artifact_chart(tool, arguments, result, ctx):
    """The model's final chart: the chart plus the table behind it."""
    from ai_agent.tools.artifacts import _normalize_chart_spec

    spec, error = _normalize_chart_spec(arguments.get("chart_spec"))
    if error:
        return None
    title = arguments.get("title") or "رسم بياني"
    labels, datasets = spec["labels"], spec["datasets"]
    columns = [schema.column("label", "البند", priority="primary")]
    columns += [schema.column(f"s{i}", d["name"] or title, "number", priority="secondary", total=True)
                for i, d in enumerate(datasets)]
    rows = []
    for li, label in enumerate(labels):
        cells = {"label": {"v": label, "d": label}}
        for i, d in enumerate(datasets):
            cells[f"s{i}"] = schema.cell(d["values"][li], columns[i + 1])
        rows.append(schema.make_row(cells, id=label))
    chart = chart_spec(CHART_KINDS.get(spec["type"], "bar"), labels,
                       [{"name": d["name"] or title, "values": d["values"]} for d in datasets], title=title)
    presentation = schema.table(title, columns, rows, variant="ranking", chart=chart,
                                paging={"total": len(rows), "shown": len(rows), "offset": 0})
    return schema.envelope(tool=tool, data={"artifact": result.get("artifact_name"), "chart_url": result.get("chart_url")},
                           summary={"text": f"{title}: {len(labels)} بند"}, presentation=presentation)
