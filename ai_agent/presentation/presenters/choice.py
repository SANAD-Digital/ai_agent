"""app.choose: a pick-one card, so the assistant asks instead of guessing (which company, which warehouse)."""

import frappe

from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t

MAX_OPTIONS = 20
SOURCES = {   # wording in English; options_from_source returns it in the viewer's language
    "companies": {"doctype": "Company", "fields": ["name", "default_currency"], "filters": {}, "icon": "building",
                  "title": "Choose a company", "subtitle": "Choose the company to continue with.", "reply": "I chose the company: {label}",
                  "meta": lambda r: r.get("default_currency")},
    "warehouses": {"doctype": "Warehouse", "fields": ["name", "company"], "filters": {"is_group": 0, "disabled": 0}, "icon": "warehouse",
                   "title": "Choose a warehouse", "subtitle": "Choose the warehouse to continue with.", "reply": "I chose the warehouse: {label}",
                   "meta": lambda r: r.get("company")},
}


def options_from_source(source, user):
    spec = dict(SOURCES[source])
    spec.update({key: t(spec[key]) for key in ("title", "subtitle", "reply")})
    rows = frappe.get_list(spec["doctype"], filters=spec["filters"], fields=spec["fields"], user=user, order_by="name asc", limit_page_length=MAX_OPTIONS)
    return spec, [{"value": r["name"], "label": r["name"], "meta": spec["meta"](r), "icon": spec["icon"]} for r in rows]


def clean_options(options):
    out = []
    for o in (options or [])[:MAX_OPTIONS]:
        if isinstance(o, dict) and o.get("label"):
            out.append({"value": str(o.get("value") or o["label"])[:140], "label": str(o["label"])[:140],
                        "meta": str(o["description"])[:140] if o.get("description") else None, "icon": None})
    return out


def build(title, options, *, subtitle=None, icon=None, reply_template=None):
    presentation = {
        "type": "choice", "title": title, "subtitle": subtitle, "icon": icon or "building", "question_id": frappe.generate_hash(length=8),
        "options": [{**o, "badge": o.get("badge")} for o in options], "multiple": False,
        "submit_label": t("Continue"), "cancel_label": t("Cancel"), "reply_template": reply_template or t("I chose: {label}"), "warnings": [], "actions": [], "insights": [],
    }
    lines = [f"{i}. {o['label']}" + (f" ({o['meta']})" if o.get("meta") else "") for i, o in enumerate(options, start=1)]
    text = f"{title}\n" + "\n".join(lines) + "\n" + t("Type the option number or name to continue.")
    return schema.envelope(tool="app.choose", data={"options": [{"value": o["value"], "label": o["label"]} for o in options]},
                           summary={"text": text, "count": len(options)}, presentation=presentation)
