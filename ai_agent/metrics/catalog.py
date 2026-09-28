"""Compact metric catalog for the model's static instructions (cached)."""

import frappe

CACHE_KEY = "ai_agent:metric_catalog"


def catalog_text():
	text = frappe.cache.get_value(CACHE_KEY)
	if text is None:
		text = _build()
		frappe.cache.set_value(CACHE_KEY, text)
	return text


def clear_cache(*args, **kwargs):
	frappe.cache.delete_value(CACHE_KEY)


def _build():
	lines = []
	for row in frappe.get_all(
		"AI Metric",
		filters={"enabled": 1},
		fields=["name", "label_ar", "description", "pending_confirmation"],
		order_by="name asc",
	):
		dims = frappe.get_all(
			"AI Metric Dimension", filters={"parent": row.name, "parenttype": "AI Metric"},
			pluck="dimension_key", order_by="idx asc",
		)
		dims_text = f" [dims: {', '.join(dims)}]" if dims else ""
		lines.append(f"- {row.name}: {row.label_ar} — {row.description or ''}{dims_text}".rstrip())
	for row in frappe.get_all(
		"AI Metric Composite", filters={"enabled": 1}, fields=["name", "label_ar", "description"], order_by="name asc"
	):
		lines.append(f"- {row.name}: {row.label_ar} — {row.description or ''} (composite)".rstrip())
	return "\n".join(lines)
