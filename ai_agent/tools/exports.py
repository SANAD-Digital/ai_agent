"""Plugin tools: hand the user a file built from rows the agent already has.

Every tool takes the same source, in order of preference:
  - `artifact`: the id of a table result from this conversation (data.query and
    reports.run return one as artifact.artifact_name). Cheapest: the rows never
    pass through the model again.
  - `query`: data.query arguments; run here with the user's own permissions.
  - `rows` (+ optional `columns`): small inline data (a few KPI lines).

Files are private and attached to the run, so only the user (and auditors
with access to the run) can download them.
"""

from __future__ import annotations

import csv
import io
import json

import frappe
from frappe import _
from frappe.utils import format_datetime, now_datetime

MAX_EXPORT_ROWS = 10000
MAX_PDF_ROWS = 2000
MAX_IMAGE_ROWS = 30
ARABIC = "؀-ۿ"


# -- source ------------------------------------------------------------------

def _source(arguments, ctx):
    """(columns [(key, label)], rows [dict]) or an error string."""
    rows, columns = None, arguments.get("columns")
    if arguments.get("artifact"):
        doc = frappe.db.get_value("AI Artifact", arguments["artifact"], ["run", "content_json", "artifact_type"], as_dict=True)
        if not doc or doc.artifact_type != "Table":
            return None, None, "artifact not found or not a table"
        owner = frappe.db.get_value("AI Run", doc.run, "user")
        if owner != getattr(ctx, "user", frappe.session.user):
            return None, None, "artifact not found or not a table"
        content = json.loads(doc.content_json or "{}")
        rows = content.get("rows") or []
        columns = columns or content.get("columns")
    elif isinstance(arguments.get("query"), dict):
        from ai_agent.tools.data import handle_query

        result = handle_query(dict(arguments["query"]), ctx)
        if result.get("error"):
            return None, None, result["error"]
        rows = result.get("rows") or []
    else:
        rows = arguments.get("rows") or []
    if not isinstance(rows, list):
        return None, None, "rows must be an array"
    rows = [r for r in rows[:MAX_EXPORT_ROWS] if isinstance(r, (dict, list))]
    if rows and isinstance(rows[0], list):
        keys = [c.get("key") if isinstance(c, dict) else str(c) for c in (columns or [])] or [f"col{i + 1}" for i in range(len(rows[0]))]
        rows = [dict(zip(keys, r)) for r in rows]
    return _columns(columns, rows, arguments.get("doctype")), rows, None


def _columns(columns, rows, doctype=None):
    from ai_agent.presentation.labels import label_for

    out = []
    for col in columns or (list(rows[0].keys()) if rows else []):
        if isinstance(col, dict):
            key = col.get("key") or col.get("fieldname") or col.get("label")
            out.append((key, col.get("label") or label_for(key, doctype)))
        else:
            out.append((str(col), label_for(str(col), doctype)))
    return out


def _is_arabic(*texts):
    import re

    return any(re.search(f"[{ARABIC}]", str(t or "")) for t in texts)


def _rtl(title, columns):
    return _is_arabic(title, *[label for _, label in columns]) or frappe.local.lang == "ar"


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _file_name(title, ext):
    import re

    base = re.sub(r"[^\w؀-ۿ -]+", "", str(title or "export")).strip().replace(" ", "_")[:60] or "export"
    return f"{base}_{now_datetime().strftime('%Y%m%d-%H%M')}.{ext}"


def _save(title, ext, content, ctx, fmt, row_count):
    run = getattr(ctx, "run_name", None)
    file_doc = frappe.get_doc({
        "doctype": "File",
        "file_name": _file_name(title, ext),
        "content": content,
        "is_private": 1,
        "attached_to_doctype": "AI Run" if run else None,
        "attached_to_name": run,
    })
    file_doc.save(ignore_permissions=True)
    return {
        "format": fmt,
        "title": title,
        "file_name": file_doc.file_name,
        "file_url": file_doc.file_url,
        "file_size": file_doc.file_size,
        "rows": row_count,
        "note": "The file is ready; the user sees a download card. Do not paste the rows again.",
    }


# -- Excel / CSV -------------------------------------------------------------

def handle_excel(arguments, ctx):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    columns, rows, error = _source(arguments, ctx)
    if error:
        return {"error": error}
    if not rows:
        return {"error": "no rows to export"}
    title = arguments.get("title") or "تصدير"
    wb = Workbook()
    ws = wb.active
    ws.title = str(title)[:31].replace("/", "-")
    ws.sheet_view.rightToLeft = _rtl(title, columns)
    ws.append([label for _, label in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="C96442")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        ws.append([_cell(row.get(key)) for key, _ in columns])
    for index, (key, label) in enumerate(columns, start=1):
        width = max([len(str(label))] + [len(str(r.get(key) or "")) for r in rows[:200]])
        ws.column_dimensions[get_column_letter(index)].width = min(max(10, width + 2), 50)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return _save(title, "xlsx", out.getvalue(), ctx, "excel", len(rows))


def handle_csv(arguments, ctx):
    columns, rows, error = _source(arguments, ctx)
    if error:
        return {"error": error}
    if not rows:
        return {"error": "no rows to export"}
    title = arguments.get("title") or "تصدير"
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _, label in columns])
    for row in rows:
        writer.writerow([_cell(row.get(key)) for key, _ in columns])
    # BOM so Excel opens Arabic text correctly
    return _save(title, "csv", ("﻿" + out.getvalue()).encode("utf-8"), ctx, "csv", len(rows))


# -- PDF ---------------------------------------------------------------------

def handle_pdf(arguments, ctx):
    columns, rows, error = _source(arguments, ctx)
    if error:
        return {"error": error}
    title = arguments.get("title") or "تقرير"
    summary = str(arguments.get("summary") or "").strip()
    if not rows and not summary:
        return {"error": "nothing to put in the PDF: give rows or a summary"}
    rows = rows[:MAX_PDF_ROWS]
    rtl = _rtl(title, columns) or _is_arabic(summary)
    meta = format_datetime(now_datetime()) + (f" · {len(rows)} {'سجل' if rtl else 'rows'}" if rows else "")
    pdf = _table_pdf(title, [meta], summary.splitlines(), columns, rows, rtl=rtl)
    return _save(title, "pdf", pdf, ctx, "pdf", len(rows))


def _ltr_dates(html):
    """Keep dates and signed numbers in their own order inside Arabic text."""
    import re

    return re.sub(r"-?\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?|-?\d[\d,]*\.\d+", lambda m: f'<span dir="ltr">{m.group(0)}</span>', html)


def _table_pdf(title, meta_lines, summary_lines, columns, rows, rtl=True, strong=None):
    """A4 PDF: title, meta lines, optional summary paragraphs and a table.
    `strong(row)` marks rows (totals, opening/closing) to print in bold."""
    from frappe.utils.pdf import get_pdf

    esc = frappe.utils.escape_html
    head = "".join(f"<th>{esc(str(label))}</th>" for _, label in columns)
    body = "".join(
        f"<tr class=\"{'strong' if strong and strong(r) else ''}\">"
        + "".join(f"<td class=\"{'num' if isinstance(r.get(k), (int, float)) else ''}\">{esc(_fmt(r.get(k)))}</td>" for k, _ in columns)
        + "</tr>"
        for r in rows
    )
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>" if rows else ""
    meta = "".join(f"<div class=\"meta\">{_ltr_dates(esc(line))}</div>" for line in meta_lines if line)
    summary_html = "".join(f"<p>{_ltr_dates(esc(line))}</p>" for line in summary_lines if str(line).strip())
    html = f"""<!doctype html><html lang="{'ar' if rtl else 'en'}" dir="{'rtl' if rtl else 'ltr'}"><head><meta charset="utf-8">
<style>
 body {{ font-family: "DejaVu Sans", sans-serif; font-size: 10.5px; color: #1f1f1c; margin: 0; }}
 h1 {{ font-size: 18px; margin: 0 0 4px; color: #c96442; }}
 .meta {{ color: #6f6e68; margin-bottom: 2px; }}
 p {{ margin: 10px 0 6px; line-height: 1.6; }}
 table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
 th {{ background: #c96442; color: #fff; padding: 6px; text-align: start; font-weight: bold; }}
 td {{ padding: 5px 6px; border-bottom: 1px solid #e3e2dd; }}
 td.num {{ text-align: {'left' if rtl else 'right'}; white-space: nowrap; direction: ltr; }}
 tr:nth-child(even) td {{ background: #f7f7f5; }}
 tr.strong td {{ font-weight: bold; background: #f7e9e3; }}
 thead {{ display: table-header-group; }} tr {{ page-break-inside: avoid; }}
</style></head><body><h1>{esc(title)}</h1>{meta}{summary_html}{table}</body></html>"""
    options = {"page-size": "A4", "orientation": "Landscape" if len(columns) > 6 else "Portrait", "margin-top": "12mm", "margin-bottom": "12mm", "margin-left": "10mm", "margin-right": "10mm", "encoding": "UTF-8"}
    return get_pdf(html, options)


# -- Print a document --------------------------------------------------------

def handle_print(arguments, ctx):
    """The document as a PDF in its print format (the one the user would print from the form)."""
    doctype, name = arguments.get("doctype"), arguments.get("name")
    if not doctype or not name or not frappe.db.exists("DocType", doctype) or not frappe.db.exists(doctype, name):
        return {"error": "document not found"}
    user = getattr(ctx, "user", frappe.session.user)
    if not frappe.has_permission(doctype, "read", doc=name, user=user) or not frappe.has_permission(doctype, "print", doc=name, user=user):
        return {"error": "no print permission on this document"}
    print_format = arguments.get("print_format") or frappe.get_meta(doctype).default_print_format or None
    if print_format and not frappe.db.exists("Print Format", {"name": print_format, "doc_type": doctype, "disabled": 0}):
        print_format = None
    pdf = frappe.get_print(doctype, name, print_format=print_format, as_pdf=True, no_letterhead=0 if arguments.get("letterhead", True) else 1)
    title = f"{_(doctype)} {name}"
    result = _save(title, "pdf", pdf, ctx, "pdf", 0)
    result["print_format"] = print_format or "Standard"
    return result


# -- Party statement ---------------------------------------------------------

STATEMENT_PARTIES = ("Customer", "Supplier", "Employee", "Shareholder")
PARTY_AR = {"Customer": "العميل", "Supplier": "المورد", "Employee": "الموظف", "Shareholder": "المساهم"}


def _resolve_party(party_type, text, user):
    """(party ID, []) for an exact ID or a single name match; (None, candidates) when it is ambiguous.
    Saves the model the lookup calls: it passes what the user typed."""
    text = str(text or "").strip()
    if not text:
        return None, []
    if frappe.db.exists(party_type, text):
        return text, []
    title = frappe.get_meta(party_type).title_field or "name"
    rows = frappe.get_list(party_type, filters={title: text}, fields=["name", title], limit=6, user=user) \
        or frappe.get_list(party_type, filters={title: ["like", f"%{text}%"]}, fields=["name", title], limit=6, user=user)
    if len(rows) == 1:
        return rows[0].name, []
    return None, [{"id": r.name, "name": r.get(title)} for r in rows]


def handle_statement(arguments, ctx):
    """Statement of account for one party from the General Ledger report (opening, entries, closing)."""
    from frappe.utils import add_months, getdate, today

    party_type = arguments.get("party_type") or "Customer"
    if party_type not in STATEMENT_PARTIES:
        return {"error": f"party_type must be one of {', '.join(STATEMENT_PARTIES)}"}
    user = getattr(ctx, "user", frappe.session.user)
    party, candidates = _resolve_party(party_type, arguments.get("party"), user)
    if not party:
        if candidates:
            return {"error": "several parties match; ask the user which one", "candidates": candidates}
        return {"error": f"no {party_type} matches '{arguments.get('party')}'"}
    if not frappe.has_permission(party_type, "read", doc=party, user=user):
        return {"error": f"no read permission on {party_type} {party}"}
    company = arguments.get("company") or frappe.defaults.get_user_default("Company", user) or frappe.db.get_single_value("Global Defaults", "default_company")
    to_date = str(getdate(arguments.get("to_date") or today()))
    from_date = str(getdate(arguments.get("from_date") or add_months(to_date, -12)))
    filters = {"company": company, "from_date": from_date, "to_date": to_date, "party_type": party_type, "party": [party],
               "group_by": "Group by Voucher (Consolidated)", "include_dimensions": 0}
    try:
        report = frappe.get_attr("frappe.desk.query_report.run")("General Ledger", filters)
    except frappe.PermissionError:
        return {"error": "no permission on the General Ledger report"}
    language = arguments.get("language") or frappe.db.get_value("User", user, "language") or frappe.local.lang
    rtl = str(language).startswith("ar")
    labels = {"'Opening'": "الرصيد الافتتاحي", "'Total'": "إجمالي الفترة", "'Closing (Opening + Total)'": "الرصيد الختامي"} if rtl else {}
    rows, opening, closing = [], 0, 0
    for row in report.get("result") or []:
        if not isinstance(row, dict):
            continue
        marker = row.get("account") if not row.get("voucher_no") else None
        if marker == "'Opening'":
            opening = row.get("balance") or 0
        if marker == "'Closing (Opening + Total)'":
            closing = row.get("balance") or 0
        rows.append({
            "posting_date": str(row.get("posting_date") or ""),
            "voucher_type": labels.get(marker, marker.strip("'") if marker else _(row.get("voucher_type") or "", lang="ar" if rtl else None)),
            "voucher_no": row.get("voucher_no") or "",
            "debit": row.get("debit") or 0.0,
            "credit": row.get("credit") or 0.0,
            "balance": row.get("balance") or 0.0,
            "_strong": bool(marker),
        })
    entries = sum(1 for r in rows if not r["_strong"])
    if rtl:
        columns = [("posting_date", "التاريخ"), ("voucher_type", "المستند"), ("voucher_no", "الرقم"), ("debit", "مدين"), ("credit", "دائن"), ("balance", "الرصيد")]
        party_name = frappe.db.get_value(party_type, party, frappe.get_meta(party_type).title_field or "name") or party
        title = f"كشف حساب {PARTY_AR.get(party_type, party_type)}: {party_name}"
        meta = [f"{company} · من {from_date} إلى {to_date}", f"{entries} حركة · طُبع {format_datetime(now_datetime())}"]
        side = "عليه" if closing > 0 else "له" if closing < 0 else ""
        summary = [f"الرصيد الافتتاحي: {_fmt(opening)} · الرصيد الختامي: {_fmt(abs(closing))} {side}".strip()]
    else:
        columns = [("posting_date", "Date"), ("voucher_type", "Voucher"), ("voucher_no", "No."), ("debit", "Debit"), ("credit", "Credit"), ("balance", "Balance")]
        title = f"Statement of Account: {party}"
        meta = [f"{company} · {from_date} to {to_date}", f"{entries} entries"]
        summary = [f"Opening: {_fmt(opening)} · Closing: {_fmt(closing)}"]
    pdf = _table_pdf(title, meta, summary, columns, rows, rtl=rtl, strong=lambda r: r.get("_strong"))
    result = _save(title, "pdf", pdf, ctx, "statement", entries)
    # The sign is easy to misread (a customer's credit balance is negative): spell it out for the model.
    side_ar = "عليه (مدين لنا)" if closing > 0 else "له (دائن، نحن مدينون له)" if closing < 0 else "صفر"
    result.update({
        "party": party, "opening": opening, "closing": closing, "from_date": from_date, "to_date": to_date, "company": company,
        "closing_text": f"{_fmt(abs(closing))} {side_ar}" if closing else "0",
        "note": "The file is ready; the user sees a download card. Report closing_text as written; do not re-derive the sign.",
    })
    return result


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


# -- Image -------------------------------------------------------------------

def _shape(text):
    """Arabic needs joined letters and right-to-left order before matplotlib draws it."""
    text = str(text if text is not None else "")
    if not _is_arabic(text):
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def handle_image(arguments, ctx):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    title = arguments.get("title") or "صورة"
    chart = arguments.get("chart")
    plt.rcParams["font.family"] = "DejaVu Sans"
    if isinstance(chart, dict):
        from ai_agent.tools.artifacts import _normalize_chart_spec

        chart, error = _normalize_chart_spec(chart)
        if error:
            return {"error": error}
        fig = _draw_chart(plt, title, chart)
        count = len(chart.get("labels") or [])
    else:
        columns, rows, error = _source(arguments, ctx)
        if error:
            return {"error": error}
        if not rows:
            return {"error": "give `chart` (a chart spec) or table rows"}
        fig = _draw_table(plt, title, columns, rows[:MAX_IMAGE_ROWS])
        count = min(len(rows), MAX_IMAGE_ROWS)
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    result = _save(title, "png", out.getvalue(), ctx, "image", count)
    result["image"] = True
    return result


PALETTE = ["#c96442", "#3b82c4", "#2f9e6e", "#d4a72c", "#8e5bd0", "#d0567a", "#5aa5a8", "#7a7a72"]


def _draw_chart(plt, title, chart):
    kind = chart.get("type") or "bar"
    labels = [_shape(l) for l in chart.get("labels") or []]
    datasets = chart.get("datasets") or []
    fig, ax = plt.subplots(figsize=(9, 5))
    if kind in ("pie", "donut", "percentage"):
        values = datasets[0].get("values") or []
        wedges = ax.pie(values, labels=labels, colors=PALETTE[: len(values)], autopct="%1.0f%%", startangle=90,
                        wedgeprops={"width": 0.45} if kind == "donut" else None)
        ax.axis("equal")
        del wedges
    else:
        import numpy as np

        x = np.arange(len(labels))
        width = 0.8 / max(1, len(datasets))
        for i, ds in enumerate(datasets):
            values = [float(v or 0) for v in ds.get("values") or []]
            color = PALETTE[i % len(PALETTE)]
            name = _shape(ds.get("name") or "")
            if kind == "line":
                ax.plot(x, values, marker="o", color=color, label=name, linewidth=2)
            else:
                ax.bar(x + (i - (len(datasets) - 1) / 2) * width, values, width, color=color, label=name)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30 if len(labels) > 6 else 0, ha="right" if len(labels) > 6 else "center")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.25)
        ax.yaxis.set_major_formatter(matplotlib_formatter())
        if len(datasets) > 1:
            ax.legend(frameon=False)
    ax.set_title(_shape(title), fontsize=14, pad=12)
    return fig


def matplotlib_formatter():
    from matplotlib.ticker import FuncFormatter

    return FuncFormatter(lambda v, _: f"{v:,.0f}")


def _draw_table(plt, title, columns, rows):
    cells = [[_shape(_fmt(r.get(k))) for k, _ in columns] for r in rows]
    headers = [_shape(label) for _, label in columns]
    rtl = _is_arabic(title, *[label for _, label in columns])
    if rtl:
        headers = headers[::-1]
        cells = [row[::-1] for row in cells]
    fig, ax = plt.subplots(figsize=(min(16, 1.8 * max(3, len(headers))), 0.42 * (len(cells) + 2) + 0.6))
    ax.axis("off")
    table = ax.table(cellText=cells, colLabels=headers, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.35)
    for (r, _c), cell in table.get_celld().items():
        cell.set_edgecolor("#e3e2dd")
        if r == 0:
            cell.set_facecolor("#c96442")
            cell.set_text_props(color="white", weight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f7f7f5")
    ax.set_title(_shape(title), fontsize=13, pad=10)
    return fig
