"""One presentation schema for every tool: builders and the result envelope."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation.labels import (
    DATE_FIELDS, PRIORITY_ORDER, field_tier, label_for, looks_like_money, status_display,
)

SCHEMA_VERSION = 1
MAX_ROWS = 500
TONES = ("success", "warning", "danger", "info", "neutral")


def column(key, label=None, type="text", *, doctype=None, align=None, priority=None,
           total=False, link_doctype=None, tooltip=None, sortable=True, currency_key=None,
           width=None, filterable=False, mobile_priority=None):
    """Column definition. type: text number currency percentage quantity date datetime status
    badge document_link boolean. priority: primary secondary supporting technical (technical
    columns are never shown by default). mobile_priority orders the fields of a row card."""
    align = align or ("end" if type in ("currency", "number", "percentage", "quantity") else "start")
    col = {
        "key": key,
        "label": label or label_for(key, doctype),
        "type": type,
        "align": align,
        "sortable": sortable,
        "filterable": bool(filterable or type in ("status", "badge")),
        "priority": priority or "supporting",
    }
    if mobile_priority:
        col["mobile_priority"] = mobile_priority
    if total:
        col["total"] = True
    if link_doctype:
        col["link_doctype"] = link_doctype
    if tooltip:
        col["tooltip"] = tooltip
    if currency_key:
        col["currency_key"] = currency_key
    if width:
        col["width"] = width
    return col


def cell(raw, column_spec, *, currency=None, zero_as_unset=False):
    """Display cell for one value, formatted according to the column type."""
    kind = column_spec["type"]
    out = {"v": raw}
    if kind == "currency":
        out["d"] = fmt.money(raw, currency, zero_as_unset=zero_as_unset)
    elif kind in ("number", "quantity"):
        out["d"] = fmt.number(raw)
    elif kind == "percentage":
        out["d"] = fmt.percent(raw)
    elif kind == "date":
        out["d"] = fmt.date(raw)
    elif kind == "datetime":
        out["d"] = fmt.datetime_text(raw)
    elif kind == "boolean":
        out["d"] = fmt.boolean(raw)
    elif kind in ("status", "badge"):
        label, tone = status_display(raw)
        out["d"] = label or fmt.NULL_TEXT
        out["tone"] = tone
    else:
        out["d"] = fmt.text(raw)
    return out


def kpi(key, label, value, *, type="currency", currency=None, tone="neutral", hint=None,
        delta=None, filter=None, exact=None, icon=None, accent=None, compact=False):
    item = {"key": key, "label": label, "type": type, "tone": tone, "v": value}
    if icon:
        item["icon"] = icon      # decorative glyph name (see ui/src/icons.js)
    if accent:
        item["accent"] = accent  # purple | teal: extra card tints beyond the semantic tones
    if type == "currency" and compact:
        # Big headline numbers read as 4.8M; the full figure stays in `exact` (tooltip / detail).
        item["d"] = f"{fmt.compact(value)} {currency}" if currency else fmt.compact(value)
        item["exact"] = fmt.money(value, currency)
    elif type == "currency":
        item["d"] = fmt.money(value, currency)
    elif type == "number":
        item["d"] = fmt.number(value)
    elif type == "percentage":
        item["d"] = fmt.percent(value)
    else:
        item["d"] = fmt.text(value)
    if exact is not None:
        item["exact"] = exact
    if hint:
        item["hint"] = hint
    if delta is not None:
        item["delta"] = {"v": delta, "d": fmt.percent(abs(delta))}
        item["delta"]["tone"] = "success" if delta > 0 else "danger" if delta < 0 else "neutral"
        item["delta"]["dir"] = "up" if delta > 0 else "down" if delta < 0 else "flat"
    if filter:
        item["filter"] = filter  # clicking the KPI applies this row filter
    return item


def warning(level, text, code=None):
    return {"level": level if level in ("info", "warning", "critical") else "info", "text": text, "code": code}


def action(key, label, *, kind="link", style="secondary", tool=None, arguments=None, href=None,
           confirm=False, overflow=False, icon=None, placement=None):
    item = {"key": key, "label": label, "kind": kind, "style": style}
    if icon:
        item["icon"] = icon
    if placement:
        item["placement"] = placement  # "header": shown beside the title instead of the action row below
    if tool:
        item["tool"] = tool
    if tool or arguments:
        item["arguments"] = arguments or {}
    if href:
        item["href"] = href
    if confirm:
        item["confirm"] = True
    if overflow:
        item["overflow"] = True
    return item


def table(title, columns, rows, *, variant="data", subtitle=None, summary=None, kpis=None,
          totals=None, insights=None, warnings=None, actions=None, filters=None, sort=None,
          density="compact", empty=None, paging=None, group_by=None, chart=None, developer=None,
          currency=None, icon=None, tabs=None, search_placeholder=None):
    return {
        "type": "table",
        "variant": variant,
        "title": title,
        "subtitle": subtitle,
        "icon": icon,
        "tabs": tabs or [],
        "search_placeholder": search_placeholder,
        "summary": summary or {},
        "kpis": kpis or [],
        "columns": columns,
        "rows": rows,
        "totals": totals,
        "filters": filters or [],
        "sort": sort,
        "density": density,
        "group_by": group_by,
        "insights": insights or [],
        "warnings": warnings or [],
        "actions": actions or [],
        "empty": empty,
        "paging": paging,
        "chart": chart,
        "currency": currency,
        "developer": developer or {},
    }


def make_row(cells, *, id=None, href=None, actions=None, group=None, level=0, kind=None, tone=None):
    row = {"cells": cells}
    if id:
        row["id"] = id
    if href:
        row["href"] = href
    if actions:
        row["actions"] = actions
    if group is not None:
        row["group"] = group
    if level:
        row["level"] = level
    if kind:
        row["kind"] = kind  # opening | closing | group | total
    if tone:
        row["tone"] = tone
    return row


def infer_column(key, values, doctype=None):
    """Type a column from its name first, then from its values."""
    non_blank = [v for v in values if not fmt.is_blank(v)]
    if key in ("status", "workflow_state", "docstatus_label"):
        return column(key, doctype=doctype, type="status")
    if DATE_FIELDS.search(key) and (not non_blank or all(fmt.as_date(v) for v in non_blank[:5])):
        return column(key, doctype=doctype, type="date")
    if not non_blank and looks_like_money(key):
        return column(key, doctype=doctype, type="currency")  # never recorded: shown as "غير مسجل", not 0
    numeric = non_blank and all(fmt.to_number(v) is not None for v in non_blank[:20])
    if numeric and looks_like_money(key):
        return column(key, doctype=doctype, type="currency", total=True)
    if numeric and any(h in key for h in ("percent", "ratio", "share")):
        return column(key, doctype=doctype, type="percentage")
    if numeric and not (key.startswith("name") or key.endswith("_no")):
        return column(key, doctype=doctype, type="number", total=key in ("qty", "actual_qty", "count"))
    return column(key, doctype=doctype)


def order_keys(keys):
    """Business order: tier 1, 2, 3; technical fields are dropped (kept in developer details)."""
    visible, technical = [], []
    for position, key in enumerate(keys):
        (technical if field_tier(key) == 4 else visible).append((field_tier(key), position, key))
    rank = {key: i for i, key in enumerate(PRIORITY_ORDER)}
    ordered = sorted(visible, key=lambda item: (item[0], rank.get(item[2], len(rank)), item[1]))
    return [k for _, _, k in ordered], [k for _, _, k in technical]


def envelope(*, tool, data, summary, presentation=None, actions=None, warnings=None, meta=None):
    """The one output contract: data / summary / presentation / actions / warnings / meta."""
    return {
        "data": data,
        "summary": summary,
        "presentation": presentation,
        "actions": actions or [],
        "warnings": warnings or [],
        "meta": {"tool": tool, "schema_version": SCHEMA_VERSION, **(meta or {})},
    }


def lite(presentation):
    """The part of a presentation the model needs: no rows, no tokens, no developer details."""
    if not isinstance(presentation, dict):
        return presentation
    # Rows, pictures and detail lists are for the view; the model gets the summary and the structure.
    drop = {"rows", "tables", "sections", "editable", "developer", "filters", "chart", "totals", "photo", "details", "documents"}
    out = {k: v for k, v in presentation.items() if k not in drop}
    if isinstance(out.get("blocked"), dict):
        out["blocked"] = {k: v for k, v in out["blocked"].items() if k != "image"}
    if presentation.get("insights"):
        out["insights"] = [i if isinstance(i, str) else {k: v for k, v in i.items() if k not in ("drill", "documents")} for i in presentation["insights"]]
    out["actions"] = [{k: v for k, v in a.items() if k != "arguments"} for a in presentation.get("actions", [])]
    if "rows" in presentation:
        out["row_count"] = len(presentation["rows"])
    if presentation.get("chart"):
        out["chart"] = {"kind": presentation["chart"]["kind"], "title": presentation["chart"].get("title")}
    if presentation.get("paging"):
        out["paging"] = {k: v for k, v in presentation["paging"].items() if k != "token"}
    return out


def insight(title, observation, *, cause=None, impact=None, action=None, documents=None, drill=None, level="info"):
    """One analytical finding: what was seen, why it may be so, what it affects, what to do, and where to look.
    Everything in it is computed from the data on the server; the assistant adds its own reading in chat."""
    item = {"title": title, "observation": observation, "level": level if level in ("info", "warning", "critical") else "info"}
    if cause:
        item["cause"] = cause
    if impact:
        item["impact"] = impact
    if action:
        item["action"] = action
    if documents:
        item["documents"] = documents[:6]
    if drill:
        item["drill"] = drill
    return item


def insight_text(item):
    return item if isinstance(item, str) else f"{item['title']}: {item['observation']}" + (f" الإجراء المقترح: {item['action']}" if item.get("action") else "")
