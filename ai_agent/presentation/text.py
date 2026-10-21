"""Markdown fallback for hosts that cannot render the MCP App (tools-only hosts, terminals)."""

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t

LEVEL_MARK = {"info": "ℹ️", "warning": "⚠️", "critical": "⛔"}


def _cell(text):
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _table(columns, rows, limit):
    cols = [c for c in columns if c["key"] not in ("_rank",)]
    if not cols or not rows:
        return []
    lines = ["| " + " | ".join(_cell(c["label"]) for c in cols) + " |",
             "|" + "|".join(" ---: " if c["align"] == "end" else " --- " for c in cols) + "|"]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(_cell(row["cells"].get(c["key"], {}).get("d")) for c in cols) + " |")
    return lines


def render(presentation, summary_text=None, *, max_rows=15):
    """Compact, ordered markdown: title, summary, KPIs, table, warnings, next steps."""
    if not presentation:
        return summary_text or ""
    kind = presentation.get("type")
    out = []
    title = presentation.get("title")
    if title:
        out.append(f"**{title}**" + (f" — {presentation['subtitle']}" if presentation.get("subtitle") else ""))
    if summary_text:
        out.append(summary_text)
    kpis = presentation.get("kpis") or []
    if kpis:
        out.append("  ·  ".join(f"{k['label']}: **{k['d']}**" for k in kpis))

    if kind == "table":
        rows = presentation.get("rows") or []
        if not rows:
            empty = presentation.get("empty") or {}
            out.append(empty.get("title") or t("No results."))
        else:
            out += [""] + _table(presentation["columns"], rows, max_rows)
            totals = presentation.get("totals")
            if totals:
                out.append("**" + totals["label"] + "**: " + fmt.comma().join(
                    f"{next((c['label'] for c in presentation['columns'] if c['key'] == k), k)} {v['d']}" for k, v in totals["cells"].items()))
            if len(rows) > max_rows:
                out.append(t("Showing {0} of {1} rows.", fmt.number(max_rows), fmt.number(len(rows))))
    elif kind == "error":
        out.append(presentation.get("reason") or "")
        if presentation.get("suggestion"):
            out.append(presentation["suggestion"])
    elif kind == "document":
        for section in presentation.get("sections") or []:
            if section.get("collapsed"):
                continue
            out.append(f"_{section['title']}_")
            out.append("- " + "\n- ".join(f"{f['label']}: {f['d']}" for f in section["fields"]))
        for table in presentation.get("tables") or []:
            out += ["", f"**{table['title']}**"] + _table(table["columns"], table["rows"], max_rows)
    elif kind == "choice":
        for i, o in enumerate(presentation.get("options") or [], start=1):
            out.append(f"{i}. {o['label']}" + (f" ({o['meta']})" if o.get("meta") else ""))
        out.append(t("Type the option number or name to continue."))
    elif kind == "approval":
        blocked = presentation.get("blocked")
        if blocked:
            out.append(f"⛔ {blocked['title']}: {blocked['message']} "
                       + t("Available: {0} of the {1} requested.", fmt.number(blocked["available"]), fmt.number(blocked["requested"])))
        cz = presentation.get("customization")
        if cz:
            # A customization request: what, how risky, what the checks found, how to undo. The code is reviewed in ERPNext.
            out.append(f"- {cz['kind_label']} · {cz['operation_label']}: {cz['target']} ({cz['risk_label']})")
            for c in cz.get("checks") or []:
                if c["level"] in ("critical", "warning"):
                    out.append(f"{LEVEL_MARK.get(c['level'], '')} {c['message']}" + (f" ({cz['labels']['line'].format(c['line'])})" if c.get("line") else ""))
            for line in cz.get("impact") or []:
                out.append(f"- {line}")
            out.append(f"- {cz['labels']['undo']}: {cz['undo']}")
        for f in presentation.get("fields") or []:
            out.append(f"- {f['label']}: {f['d']}")
        for f in (presentation.get("impact_financial") or []) + (presentation.get("impact_stock") or []):
            out.append(f"- {f['label']}: {f['d']}")
        if presentation.get("next_steps"):
            out.append(t("After approval:") + " " + " ".join(presentation["next_steps"]))
        for d in presentation.get("documents") or []:
            out.append(f"- {d['type_label']}: {d['title']}" + (" (" + fmt.comma().join(f"{x['label']}: {x['d']}" for x in d["fields"][:4]) + ")" if d["fields"] else ""))
        for it in presentation.get("items") or []:
            out.append(f"- {it['item']} × {it['d']['qty']} @ {it['d']['rate']} = {it['d']['amount']}")
        if presentation.get("result") and presentation["result"].get("document_name"):
            out.append(t("Document: {0} — {1}", presentation["result"]["document_name"], presentation["result"]["href"]))
        elif presentation.get("state") == "pending":
            out.append(t("Status: awaiting approval (request {0}).", presentation["approval_id"]))
            out.append(presentation["fallback"]["text"])
    for w in presentation.get("warnings") or []:
        out.append(f"{LEVEL_MARK.get(w['level'], '')} {w['text']}")
    for insight in presentation.get("insights") or []:
        out.append("💡 " + schema.insight_text(insight))
    return "\n".join(x for x in out if x is not None).strip()
