"""app.choose: a pick-one card, so the assistant asks instead of guessing (which company, which warehouse)."""

import frappe

from ai_agent.presentation import schema

MAX_OPTIONS = 20
SOURCES = {
    "companies": {"doctype": "Company", "fields": ["name", "default_currency"], "filters": {}, "icon": "building",
                  "title": "اختر الشركة", "subtitle": "يرجى اختيار الشركة المناسبة للمتابعة.", "reply": "اخترت الشركة: {label}",
                  "meta": lambda r: r.get("default_currency")},
    "warehouses": {"doctype": "Warehouse", "fields": ["name", "company"], "filters": {"is_group": 0, "disabled": 0}, "icon": "warehouse",
                   "title": "اختر المخزن", "subtitle": "يرجى اختيار المخزن المناسب للمتابعة.", "reply": "اخترت المخزن: {label}",
                   "meta": lambda r: r.get("company")},
}


def options_from_source(source, user):
    spec = SOURCES[source]
    rows = frappe.get_list(spec["doctype"], filters=spec["filters"], fields=spec["fields"], user=user, order_by="name asc", limit_page_length=MAX_OPTIONS)
    return spec, [{"value": r["name"], "label": r["name"], "meta": spec["meta"](r), "icon": spec["icon"]} for r in rows]


def clean_options(options):
    out = []
    for o in (options or [])[:MAX_OPTIONS]:
        if isinstance(o, dict) and o.get("label"):
            out.append({"value": str(o.get("value") or o["label"])[:140], "label": str(o["label"])[:140],
                        "meta": str(o["description"])[:140] if o.get("description") else None, "icon": None})
    return out


def build(title, options, *, subtitle=None, icon=None, reply_template="اخترت: {label}"):
    presentation = {
        "type": "choice", "title": title, "subtitle": subtitle, "icon": icon or "building", "question_id": frappe.generate_hash(length=8),
        "options": [{**o, "badge": o.get("badge")} for o in options], "multiple": False,
        "submit_label": "متابعة", "cancel_label": "إلغاء", "reply_template": reply_template, "warnings": [], "actions": [], "insights": [],
    }
    lines = [f"{i}. {o['label']}" + (f" ({o['meta']})" if o.get("meta") else "") for i, o in enumerate(options, start=1)]
    text = f"{title}\n" + "\n".join(lines) + "\nاكتب رقم الخيار أو اسمه للمتابعة."
    return schema.envelope(tool="app.choose", data={"options": [{"value": o["value"], "label": o["label"]} for o in options]},
                           summary={"text": text, "count": len(options)}, presentation=presentation)
