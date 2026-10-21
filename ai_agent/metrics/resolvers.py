"""Registered resolvers for metrics/dimensions a single aggregate cannot express.

Dimension resolver: (definition, period, dim, other_dims, filters, company) -> result
Metric resolver:    (definition, period, dimension_keys, filters, company) -> result
where result = {"dimensions", "rows", "total", "source", "notes"?}. Every read
goes through frappe.get_list in the caller's session.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate

CREDIT_LABEL = "آجل"
MAX_GROUPS = 50


def _engine():
	from ai_agent.metrics import engine

	return engine


def _sum(doctype, field, filters, **kwargs):
	rows = frappe.get_list(doctype, filters=filters, fields=[{"SUM": field, "as": "v"}], limit_page_length=0, **kwargs)
	return flt(rows[0].get("v")) if rows else 0.0


def _combos(definition, others, base_filters):
	"""Distinct values of the other (document-level) dimensions, as extra filters."""
	if not others:
		return [({}, [])]
	engine = _engine()
	fields = [d.field_path for d in others]
	rows = frappe.get_list(
		definition.doctype,
		filters=base_filters,
		fields=[f"{f} as {engine._alias(f)}" if "." in f else f for f in fields],
		group_by=", ".join(fields),
		limit_page_length=MAX_GROUPS,
	)
	combos = []
	for row in rows:
		labels = {d.key: engine._plain(row.get(engine._alias(f))) for d, f in zip(others, fields)}
		extra = [[f, "=", row.get(engine._alias(f))] if row.get(engine._alias(f)) is not None else [f, "is", "not set"]
				 for f in fields]
		combos.append((labels, extra))
	return combos


def payment_mode(definition, period_info, dim, others, filters, company):
	"""Sales by how they were paid: POS/in-invoice payments by Mode of Payment,
	everything not paid inside the invoice as a single "آجل" (credit) bucket.

	Amounts are invoice grand totals in company currency (tax included), since
	payments settle the grand total. Change given back is deducted from the
	cash mode.
	"""
	engine = _engine()
	if definition.doctype not in ("Sales Invoice", "POS Invoice"):
		raise engine.MetricError("payment_mode applies to sales invoices only.")
	base = engine.build_filters(definition, period_info, filters, company)
	payment_doctype = frappe.get_meta(definition.doctype).get_field("payments").options
	rows = []
	for labels, extra in _combos(definition, others, base):
		scoped = base + extra
		pos = scoped + [["is_pos", "=", 1]]
		paid_rows = frappe.get_list(
			payment_doctype,
			parent_doctype=definition.doctype,
			filters=[[definition.doctype, *f] for f in pos] + [["parentfield", "=", "payments"]],
			fields=["mode_of_payment", {"SUM": "base_amount", "as": "v"}],
			group_by="mode_of_payment",
			limit_page_length=0,
		)
		change = _sum(definition.doctype, "base_change_amount", pos)
		pos_total = _sum(definition.doctype, "base_grand_total", pos)
		credit_total = _sum(definition.doctype, "base_grand_total", scoped + [["is_pos", "=", 0]])

		by_mode = {row.mode_of_payment: flt(row.v) for row in paid_rows if row.mode_of_payment}
		cash_mode = next(
			(m for m in by_mode if frappe.get_cached_value("Mode of Payment", m, "type") == "Cash"), None
		)
		if cash_mode and change:
			by_mode[cash_mode] -= change
		unpaid_pos = pos_total - sum(by_mode.values())
		credit = credit_total + max(unpaid_pos, 0)
		if credit:
			by_mode[CREDIT_LABEL] = by_mode.get(CREDIT_LABEL, 0) + credit
		for mode, value in by_mode.items():
			if value:
				rows.append({**labels, dim.key: mode, "value": round(value * definition.sign, 2)})

	dims = [d.key for d in others] + [dim.key]
	return {
		"dimensions": dims,
		"rows": rows,
		"total": round(sum(r["value"] for r in rows), 2),
		"source": {"doctype": definition.doctype, "documents": engine._document_count(definition, base)},
		"notes": [_("The payment-mode split is computed on the invoice grand total including tax; invoices not paid inside the invoice = {0}.").format(CREDIT_LABEL)],
	}


AGE_BUCKETS = (
	(None, 0, "غير مستحق"),
	(1, 30, "1-30 يوم"),
	(31, 60, "31-60 يوم"),
	(61, 90, "61-90 يوم"),
	(91, None, "أكثر من 90 يوم"),
)


def age_bucket(definition, period_info, dim, others, filters, company):
	"""Group an outstanding amount by days past its due date (as of the period end)."""
	engine = _engine()
	as_of = getdate(period_info["to"]) if period_info else getdate()
	due_field = dim.field_path or "due_date"
	result = engine.aggregate(
		definition, period_info, [*others, frappe._dict(key="__due", field_path=due_field, value_field="", resolver="")],
		filters, company,
	)
	grouped = {}
	for row in result["rows"]:
		overdue = date_diff(as_of, row.pop("__due")) if row.get("__due") else 0
		bucket = next(label for low, high, label in AGE_BUCKETS if (low is None and overdue <= 0)
					  or (low is not None and overdue >= low and (high is None or overdue <= high)))
		key = tuple(row.get(d.key) for d in others) + (bucket,)
		grouped[key] = grouped.get(key, 0) + row["value"]
	order = {label: i for i, (_, _, label) in enumerate(AGE_BUCKETS)}
	rows = [
		{**{d.key: key[i] for i, d in enumerate(others)}, dim.key: key[-1], "value": round(value, 2)}
		for key, value in sorted(grouped.items(), key=lambda kv: (kv[0][:-1], order[kv[0][-1]]))
	]
	return {
		"dimensions": [d.key for d in others] + [dim.key],
		"rows": rows,
		"total": round(sum(r["value"] for r in rows), 2),
		"source": result["source"],
		"notes": [_("Ages are counted from the due date up to {0}.").format(as_of)],
	}


def below_reorder(definition, period_info, dimension_keys, filters, company):
	"""Items whose projected quantity is below the warehouse re-order level."""
	engine = _engine()
	levels = frappe.get_list(
		"Item Reorder",
		parent_doctype="Item",
		filters=[["Item", "disabled", "=", 0], ["parentfield", "=", "reorder_levels"]],
		fields=["parent", "warehouse", "warehouse_reorder_level"],
		limit_page_length=0,
	)
	levels = {(r.parent, r.warehouse): flt(r.warehouse_reorder_level) for r in levels if flt(r.warehouse_reorder_level)}
	notes = []
	rows = []
	if not levels:
		notes.append(_("No re-order levels are defined in the Item Reorder table for any item."))
	else:
		bin_filters = [["item_code", "in", sorted({item for item, _ in levels})]]
		if company:
			bin_filters.append(["warehouse.company", "=", company])
		for key, value in (filters or {}).items():
			if key in ("item", "warehouse"):
				bin_filters.append(engine._filter_for("item_code" if key == "item" else "warehouse", value))
		bins = frappe.get_list(
			"Bin", filters=bin_filters, fields=["item_code", "warehouse", "projected_qty", "actual_qty"],
			limit_page_length=0,
		)
		for row in bins:
			level = levels.get((row.item_code, row.warehouse))
			if level and flt(row.projected_qty) < level:
				rows.append({
					"item": row.item_code, "warehouse": row.warehouse, "value": flt(row.projected_qty),
					"actual_qty": flt(row.actual_qty), "reorder_level": level,
				})
	return {
		"dimensions": ["item", "warehouse"],
		"rows": rows,
		"total": len(rows),
		"source": {"doctype": "Bin + Item Reorder", "documents": len(levels)},
		"notes": notes + [_("value = projected quantity (projected_qty) compared with the warehouse re-order level.")],
	}


DIMENSION_RESOLVERS = {
	"payment_mode": payment_mode,
	"age_bucket": age_bucket,
}
METRIC_RESOLVERS = {
	"below_reorder": below_reorder,
}
