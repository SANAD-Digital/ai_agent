"""Semantic metric layer: answers business questions from AI Metric definitions.

All reads go through frappe.get_list in the caller's session, so User
Permissions (company, branch...), permission-query hooks and role
permissions apply exactly as in the desk. Output is compact by design:
grouped rows, the concrete period, the source and the assumption line.
"""

import ast
import json
import operator

import frappe
from frappe import _
from frappe.utils import cint, flt

from ai_agent.metrics import periods
from ai_agent.metrics.resolvers import DIMENSION_RESOLVERS, METRIC_RESOLVERS

MAX_ROWS = 50
SORTS = ("value_desc", "value_asc", "dimension_asc", "dimension_desc")


class MetricError(frappe.ValidationError):
	pass


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def get_definition(metric_key):
	"""Metric definition as a plain dict, or None."""
	if not frappe.db.exists("AI Metric", metric_key):
		return None
	doc = frappe.get_cached_doc("AI Metric", metric_key)
	return frappe._dict(
		key=doc.metric_key,
		label=doc.label_ar or doc.label_en or doc.metric_key,
		enabled=cint(doc.enabled),
		pending=cint(doc.pending_confirmation),
		roles=[r.strip() for r in (doc.roles or "").splitlines() if r.strip()],
		doctype=doc.source_doctype,
		aggregate=doc.aggregate or "sum",
		value_field=(doc.value_field or "").strip(),
		sign=cint(doc.sign) or 1,
		date_field=(doc.date_field or "").strip(),
		time_field=(doc.time_field or "").strip(),
		company_field=(doc.company_field or "").strip(),
		resolver=(doc.resolver or "").strip(),
		base_filters=parse_filters(doc.base_filters),
		currency_note=doc.currency_note,
		assumption=doc.assumption_text,
		dimensions={
			row.dimension_key: frappe._dict(
				key=row.dimension_key,
				label=row.label_ar or row.dimension_key,
				field_path=(row.field_path or "").strip(),
				value_field=(row.value_field or "").strip(),
				resolver=(row.resolver or "").strip(),
			)
			for row in doc.dimensions
		},
	)


def parse_filters(raw):
	if not raw:
		return []
	value = json.loads(raw) if isinstance(raw, str) else raw
	if isinstance(value, dict):
		return [[k, "=", v] for k, v in value.items()]
	return [list(row) for row in value]


def unknown_metric_message():
	"""Name the metrics that exist: the model guessed names and retried the same call up to 7 times."""
	keys = frappe.get_all("AI Metric", filters={"enabled": 1}, pluck="name", order_by="name asc", limit=60)
	keys += frappe.get_all("AI Metric Composite", filters={"enabled": 1}, pluck="name", order_by="name asc", limit=20)
	if not keys:
		return "Unknown or disabled metric. No metrics are defined on this site: use data.query or reports.run."
	return f"Unknown or disabled metric. Available metrics: {', '.join(keys)}."


def assert_can_use(definition, user):
	if not definition or not definition.enabled:
		raise MetricError(unknown_metric_message())
	if definition.roles and not set(definition.roles) & set(frappe.get_roles(user)):
		raise MetricError(f"Metric '{definition.key}' is restricted to other roles.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def query(metric, period="this_month", dimensions=None, filters=None, compare=None,
		  top=None, company=None, sort=None, user=None):
	user = user or frappe.session.user
	if not metric:
		raise MetricError("metric is required.")
	if frappe.db.exists("AI Metric Composite", metric):
		return query_composite(metric, period, compare=compare, company=company, user=user)

	definition = get_definition(metric)
	assert_can_use(definition, user)
	company = company or None
	period_info = periods.resolve_period(period or "this_month", company)
	dimensions = [d for d in (dimensions or []) if d]
	filters = filters or {}
	if sort and sort not in SORTS:
		raise MetricError(f"sort must be one of {', '.join(SORTS)}.")

	# A dimension whose field does not exist on this site is dropped with a
	# note, so the rest of the question still gets answered.
	missing = [key for key in dimensions if _is_unconfigured(definition, key)]
	dimensions = [key for key in dimensions if key not in missing]

	result = compute(definition, period_info, dimensions, filters, company)
	output = {
		"metric": definition.key,
		"label": definition.label,
		"period": _period_output(definition, period_info),
		"company": company or _("All permitted companies"),
		"dimensions": result["dimensions"],
		"rows": result["rows"],
		"total": result["total"],
		"source": result["source"],
		"assumption": definition.assumption or "",
	}
	if definition.currency_note:
		output["currency"] = definition.currency_note
	notes = list(result.get("notes") or [])
	notes += [_("The dimension '{0}' is not configured on this site; it was not used for grouping.").format(key) for key in missing]
	if notes:
		output["notes"] = notes
	if definition.pending:
		output["pending_confirmation"] = True

	if compare:
		compare_info = periods.comparison_period(period_info, compare, company)
		previous = compute(definition, compare_info, result["dimensions"], filters, company)
		output["compare"] = {
			"mode": compare,
			"range": periods.describe(compare_info),
			"total": previous["total"],
			"change": _change(result["total"], previous["total"]),
		}
		_merge_previous(output["rows"], previous["rows"], result["dimensions"])
		if compare == "yesterday_same_time":
			full_day = dict(compare_info, until_time=None)
			output["compare"]["full_day_total"] = compute(definition, full_day, [], filters, company)["total"]

	output["rows"] = _sort_and_trim(output["rows"], result["dimensions"], sort, top)
	return output


def _is_unconfigured(definition, key):
	dim = definition.dimensions.get(key)
	return bool(dim and not dim.resolver and not _field_exists(definition.doctype, dim.field_path))


def _period_output(definition, period_info):
	if not definition.date_field and not definition.resolver:
		return {"key": "now", "range": _("Current balance")}
	if definition.aggregate == "balance":
		return {"key": period_info["key"], "range": _("Up to {0}").format(period_info["to"])}
	return {"key": period_info["key"], "range": periods.describe(period_info)}


def query_composite(key, period, compare=None, company=None, user=None):
	doc = frappe.get_cached_doc("AI Metric Composite", key)
	if not cint(doc.enabled):
		raise MetricError(unknown_metric_message())
	period_info = periods.resolve_period(period or "this_month", company)
	values, parts = {}, []
	for item in doc.items:
		definition = get_definition(item.metric)
		assert_can_use(definition, user)
		alias = item.alias or item.metric
		current = compute(definition, period_info, [], {}, company)["total"]
		part = {"metric": item.metric, "label": definition.label, "value": current}
		if compare:
			compare_info = periods.comparison_period(period_info, compare, company)
			previous = compute(definition, compare_info, [], {}, company)["total"]
			part.update({"previous": previous, "change": _change(current, previous)})
		values[alias] = current
		parts.append(part)

	output = {
		"metric": doc.composite_key,
		"label": doc.label_ar,
		"period": {"key": period_info["key"], "range": periods.describe(period_info)},
		"company": company or _("All permitted companies"),
		"assumption": doc.assumption_text or "",
	}
	if compare:
		output["compare_range"] = periods.describe(periods.comparison_period(period_info, compare, company))
	if doc.composite_type == "formula":
		output["value"] = evaluate_formula(doc.formula, values)
		output["components"] = parts
		if compare:
			previous_values = {
				(item.alias or item.metric): part["previous"] for item, part in zip(doc.items, parts)
			}
			previous = evaluate_formula(doc.formula, previous_values)
			output["previous"] = previous
			output["change"] = _change(output["value"], previous)
	else:
		output["metrics"] = parts
	if cint(doc.pending_confirmation):
		output["pending_confirmation"] = True
	return output


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------


def compute(definition, period_info, dimension_keys, filters, company):
	if definition.resolver:
		resolver = METRIC_RESOLVERS.get(definition.resolver)
		if not resolver:
			raise MetricError(f"Metric resolver '{definition.resolver}' is not registered.")
		return resolver(definition, period_info, dimension_keys, filters, company)

	dims = [_dimension(definition, key) for key in dimension_keys]
	resolver_dims = [d for d in dims if d.resolver]
	if len(resolver_dims) > 1:
		raise MetricError("Only one computed dimension can be used per query.")
	if resolver_dims:
		resolver = DIMENSION_RESOLVERS.get(resolver_dims[0].resolver)
		if not resolver:
			raise MetricError(f"Dimension resolver '{resolver_dims[0].resolver}' is not registered.")
		others = [d for d in dims if not d.resolver]
		return resolver(definition, period_info, resolver_dims[0], others, filters, company)
	return aggregate(definition, period_info, dims, filters, company)


def aggregate(definition, period_info, dims, filters, company, extra_filters=None, value_override=None):
	"""One grouped aggregate through frappe.get_list. Returns rows/total/source."""
	child_tables = {d.field_path.split(".", 1)[0] for d in dims if _is_child_path(definition.doctype, d.field_path)}
	if len(child_tables) > 1:
		raise MetricError("Dimensions from different child tables cannot be combined.")

	parent_filters = build_filters(definition, period_info, filters, company) + list(extra_filters or [])
	child_filters = [f for f in parent_filters if _is_child_path(definition.doctype, f[0])]
	parent_filters = [f for f in parent_filters if f not in child_filters]

	if child_tables:
		table = child_tables.pop()
		if any(not _is_child_path(definition.doctype, d.field_path) for d in dims):
			raise MetricError("A child-table dimension cannot be combined with document-level dimensions.")
		child_doctype = frappe.get_meta(definition.doctype).get_field(table).options
		value_field = value_override or next((d.value_field for d in dims if d.value_field), "")
		if definition.aggregate != "count" and not value_field:
			raise MetricError("This dimension has no child value field configured.")
		group_fields = [d.field_path.split(".", 1)[1] for d in dims]
		query_filters = [[definition.doctype, *flt] for flt in parent_filters] + [
			[f[0].split(".", 1)[1], *f[1:]] for f in child_filters
		]
		rows = _grouped(child_doctype, definition, group_fields, value_field, query_filters,
						parent_doctype=definition.doctype, parentfield=table)
	else:
		if child_filters:
			parent_filters += [[_child_doctype(definition.doctype, f[0]), f[0].split(".", 1)[1], *f[1:]] for f in child_filters]
		group_fields = [d.field_path for d in dims]
		rows = _grouped(definition.doctype, definition, group_fields, value_override or definition.value_field, parent_filters)

	out_rows = []
	for row in rows:
		item = {d.key: _plain(row.get(_alias(field))) for d, field in zip(dims, group_fields)}
		item["value"] = round(flt(row.get("value")) * definition.sign, 2) + 0.0  # no -0.0
		out_rows.append(item)
	total = round(sum(r["value"] for r in out_rows), 2) + 0.0
	return {
		"dimensions": [d.key for d in dims],
		"rows": out_rows,
		"total": total,
		"source": {"doctype": definition.doctype, "documents": _document_count(definition, parent_filters)},
	}


def _grouped(doctype, definition, group_fields, value_field, filters, parent_doctype=None, parentfield=None):
	fields = [f"{field} as {_alias(field)}" if "." in field else field for field in group_fields]
	value_parts = _value_parts(value_field)
	agg = {"sum": "SUM", "balance": "SUM", "avg": "AVG", "count": "COUNT"}[definition.aggregate]
	if definition.aggregate == "count":
		fields.append({"COUNT": "name", "as": "v0"})
	else:
		fields += [{agg: part, "as": f"v{i}"} for i, (part, _) in enumerate(value_parts)]
	kwargs = {"filters": filters, "fields": fields, "limit_page_length": 0}
	if group_fields:
		kwargs["group_by"] = ", ".join(group_fields)
	if parent_doctype:
		kwargs["parent_doctype"] = parent_doctype
		kwargs["filters"] = list(filters) + [["parentfield", "=", parentfield]]
	try:
		rows = frappe.get_list(doctype, **kwargs)
	except frappe.PermissionError:
		raise MetricError(f"You do not have permission to read {parent_doctype or doctype}.")
	for row in rows:
		if definition.aggregate == "count":
			row["value"] = flt(row.get("v0"))
		else:
			row["value"] = sum(flt(row.get(f"v{i}")) * sign for i, (_, sign) in enumerate(value_parts))
	return rows


def build_filters(definition, period_info, filters, company):
	"""Base filters + period + company + user dimension filters, as [field, op, value]."""
	result = [list(f) for f in definition.base_filters]
	if definition.date_field and period_info:
		if definition.aggregate == "balance":
			result.append([definition.date_field, "<=", period_info["to"]])
		else:
			result.append([definition.date_field, ">=", period_info["from"]])
			result.append([definition.date_field, "<=", period_info["to"]])
			if period_info.get("until_time") and definition.time_field and period_info["from"] == period_info["to"]:
				result.append([definition.time_field, "<=", str(period_info["until_time"])])
	if company and definition.company_field:
		result.append([definition.company_field, "=", company])
	for key, value in (filters or {}).items():
		dim = _dimension(definition, key)
		if dim.resolver:
			raise MetricError(f"Filtering by '{key}' is not supported; group by it instead.")
		result.append(_filter_for(dim.field_path, value))
	return result


def _filter_for(field, value):
	if isinstance(value, (list, tuple)):
		return [field, "in", list(value)]
	if isinstance(value, dict):
		return [field, value.get("operator") or "=", value.get("value")]
	if isinstance(value, str) and "%" in value:
		return [field, "like", value]
	return [field, "=", value]


def _dimension(definition, key):
	if key == "company" and "company" not in definition.dimensions and definition.company_field:
		return frappe._dict(key="company", label=_("Company"), field_path=definition.company_field, value_field="", resolver="")
	dim = definition.dimensions.get(key)
	if not dim:
		allowed = ", ".join(sorted(definition.dimensions)) or "none"
		raise MetricError(f"Dimension '{key}' is not available for metric '{definition.key}'. Allowed: {allowed}.")
	if not dim.resolver and not _field_exists(definition.doctype, dim.field_path):
		raise MetricError(f"Dimension '{key}' is not configured on this site (field '{dim.field_path}' is missing).")
	return dim


def _field_exists(doctype, path):
	meta = frappe.get_meta(doctype)
	head, _, tail = (path or "").partition(".")
	field = meta.get_field(head)
	if not tail:
		return bool(field) or head in ("name", "owner", "creation", "modified")
	if not field or not field.options:
		return False
	return bool(frappe.get_meta(field.options).get_field(tail))


def _is_child_path(doctype, path):
	head, sep, _ = (path or "").partition(".")
	if not sep:
		return False
	field = frappe.get_meta(doctype).get_field(head)
	return bool(field and field.fieldtype in frappe.model.table_fields)


def _child_doctype(doctype, path):
	return frappe.get_meta(doctype).get_field(path.split(".", 1)[0]).options


def _value_parts(value_field):
	"""'debit - credit' -> [('debit', 1), ('credit', -1)]."""
	if not value_field:
		return [("name", 1)]
	parts, sign = [], 1
	for token in value_field.replace("+", " + ").replace("-", " - ").split():
		if token in "+-":
			sign = -1 if token == "-" else 1
			continue
		parts.append((token, sign))
		sign = 1
	return parts


def _alias(field):
	return field.replace(".", "__")


def _plain(value):
	if hasattr(value, "isoformat"):
		return value.isoformat()
	return value


def _document_count(definition, parent_filters):
	try:
		rows = frappe.get_list(
			definition.doctype, filters=parent_filters, fields=[{"COUNT": "name", "as": "n"}], limit_page_length=0
		)
		return cint(rows[0].get("n")) if rows else 0
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"AI metric document count failed: {definition.doctype}")
		return None


def _change(current, previous):
	current, previous = flt(current), flt(previous)
	change = {"amount": round(current - previous, 2)}
	if previous:
		change["percent"] = round((current - previous) * 100 / abs(previous), 1)
	return change


def _merge_previous(rows, previous_rows, dims):
	index = {tuple(r.get(d) for d in dims): r["value"] for r in previous_rows}
	for row in rows:
		prev = index.get(tuple(row.get(d) for d in dims), 0)
		row["previous"] = prev
		row["change"] = _change(row["value"], prev)


def _sort_and_trim(rows, dims, sort, top):
	sort = sort or "value_desc"
	if sort.startswith("value"):
		rows = sorted(rows, key=lambda r: flt(r["value"]), reverse=sort == "value_desc")
	elif dims:
		rows = sorted(rows, key=lambda r: str(r.get(dims[0]) or ""), reverse=sort == "dimension_desc")
	limit = min(cint(top) or MAX_ROWS, MAX_ROWS)
	return rows[:limit]


# ---------------------------------------------------------------------------
# Composite formulas
# ---------------------------------------------------------------------------

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def evaluate_formula(formula, values):
	"""Evaluate +, -, *, / over aliases and numbers; nothing else is allowed."""

	def visit(node):
		if isinstance(node, ast.Expression):
			return visit(node.body)
		if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
			left, right = visit(node.left), visit(node.right)
			if isinstance(node.op, ast.Div) and not right:
				return None
			if left is None or right is None:
				return None
			return _OPS[type(node.op)](left, right)
		if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
			value = visit(node.operand)
			return None if value is None else -value
		if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
			return node.value
		if isinstance(node, ast.Name):
			if node.id not in values:
				raise MetricError(f"Formula uses unknown alias '{node.id}'.")
			return flt(values[node.id])
		raise MetricError("Formula may only use + - * / aliases and numbers.")

	result = visit(ast.parse(str(formula or "0"), mode="eval"))
	return None if result is None else round(result, 2)
