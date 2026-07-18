import frappe
from frappe import _
from frappe.utils import cint
import json
from ai_agent.runtime.artifact_service import ArtifactService


def handle_search(arguments, permission_context):
    """Handle reports.search tool call."""
    query = (
        arguments.get("query")
        or arguments.get("keyword")
        or arguments.get("search_term")
        or arguments.get("text")
        or arguments.get("search")
        or ""
    ).lower()
    limit = min(arguments.get("limit", 20), 50)

    reports = frappe.get_all("Report", fields=["name", "report_type", "ref_doctype", "module"])
    results = []
    for r in reports:
        if not permission_context.has_doctype_permission(r.ref_doctype, "read"):
            continue
        name_lower = r.name.lower()
        if query and query not in name_lower:
            continue
        results.append({
            "name": r.name,
            "report_type": r.report_type,
            "ref_doctype": r.ref_doctype,
            "module": r.module,
        })
        if len(results) >= limit:
            break

    return {"reports": results, "count": len(results)}


def handle_describe(arguments, permission_context):
    """Handle reports.describe tool call."""
    report_name = arguments.get("report_name") or arguments.get("name") or arguments.get("report")
    if not report_name:
        return {"error": "report_name is required"}

    try:
        report = frappe.get_doc("Report", report_name)
    except frappe.DoesNotExistError:
        return {"error": f"Report '{report_name}' not found"}

    filters = []
    if hasattr(report, "filters_json") and report.filters_json:
        try:
            filters = frappe.parse_json(report.filters_json)
        except Exception:
            pass
    elif hasattr(report, "json") and report.json:
        try:
            filters = frappe.parse_json(report.json)
        except Exception:
            pass
    elif hasattr(report, "filters") and report.filters:
        for f in report.filters:
            filters.append({
                "fieldname": f.fieldname,
                "label": f.label,
                "fieldtype": f.fieldtype,
                "options": f.options,
                "default": f.default,
                "reqd": f.reqd
            })

    columns = []
    if report.columns:
        for col in report.columns.split("\n"):
            if col.strip():
                columns.append(col.strip())

    return {
        "name": report_name,
        "report_type": report.report_type,
        "ref_doctype": report.ref_doctype,
        "prepared_report": bool(report.prepared_report),
        "filters": filters,
        "columns": columns,
    }


def handle_run(arguments, permission_context):
    """Handle reports.run tool call."""
    report_name = arguments.get("report_name") or arguments.get("name") or arguments.get("report")
    filters = arguments.get("filters", {})
    sample_limit = min(cint(arguments.get("sample_limit") or 20), 50)
    include_raw_rows = bool(arguments.get("include_raw_rows"))
    prepared_report_name = arguments.get("prepared_report_name")
    generate_if_missing = arguments.get("generate_if_missing")
    if generate_if_missing is None:
        generate_if_missing = True

    if not report_name:
        return {"error": "report_name is required"}

    try:
        report = frappe.get_doc("Report", report_name)
    except frappe.DoesNotExistError:
        return {"error": f"Report '{report_name}' not found"}

    if report.ref_doctype and not permission_context.has_doctype_permission(report.ref_doctype, "read"):
        return {"error": f"No permission to run report on '{report.ref_doctype}'"}

    try:
        filters = _normalize_filters(filters)
        if prepared_report_name:
            filters["prepared_report_name"] = prepared_report_name

        result = frappe.get_attr("frappe.desk.query_report.run")(report_name, filters)
        report_result = _build_report_result(
            report_name,
            filters,
            result,
            sample_limit,
            include_raw_rows,
            getattr(permission_context, "run_name", None),
        )

        if _is_waiting_for_prepared_report(report, result, report_result):
            return _handle_missing_prepared_report(
                report_name,
                filters,
                permission_context,
                generate_if_missing,
            )

        return report_result
    except Exception as e:
        return {"error": f"Report execution failed: {str(e)}"}


def _normalize_filters(filters):
    if isinstance(filters, str):
        return frappe.parse_json(filters) or {}
    if not isinstance(filters, dict):
        return {}
    return dict(filters)


def _build_report_result(report_name, filters, result, sample_limit, include_raw_rows, run_name=None):
    rows = _rows_as_dicts(result.get("result", []), result.get("columns", []))
    columns = result.get("columns", [])
    normalized_columns = _normalize_columns(columns)
    summary = _summarize_report_rows(rows, normalized_columns)
    sample_rows = rows[:sample_limit] if include_raw_rows else _compact_rows(rows, summary, sample_limit)
    prepared_doc = result.get("doc")
    artifact_info = _create_report_artifact(
        run_name,
        report_name,
        filters,
        rows,
        normalized_columns,
        sample_limit,
    )

    return {
        "report": report_name,
        "filters": _filters_without_prepared_name(filters),
        "row_count": len(rows),
        "summary": summary,
        "result": sample_rows,
        "is_truncated": len(rows) > len(sample_rows),
        "columns": normalized_columns[:30],
        "payload_mode": "raw_rows" if include_raw_rows else "compact_summary",
        "prepared_report": bool(result.get("prepared_report")),
        "prepared_report_name": prepared_doc.name if prepared_doc else filters.get("prepared_report_name"),
        "prepared_status": prepared_doc.status if prepared_doc else None,
        "artifact": artifact_info,
    }


def _create_report_artifact(run_name, report_name, filters, rows, normalized_columns, sample_limit):
    if not run_name or len(rows) <= max(cint(sample_limit), 20):
        return None

    columns = [
        column.get("label") or column.get("fieldname")
        for column in normalized_columns
        if column.get("label") or column.get("fieldname")
    ]
    fieldnames = [
        column.get("fieldname")
        for column in normalized_columns
        if column.get("label") or column.get("fieldname")
    ]

    artifact_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        artifact_rows.append({
            columns[index]: row.get(fieldname)
            for index, fieldname in enumerate(fieldnames)
            if fieldname and index < len(columns)
        })

    if not artifact_rows:
        return None

    artifact = ArtifactService().create_table(
        run_name,
        report_name,
        artifact_rows,
        columns,
        total_row_count=len(rows),
        summary=json.dumps(_filters_without_prepared_name(filters), ensure_ascii=False, default=str),
    )
    return {
        "artifact_name": artifact.name,
        "title": artifact.title,
        "row_count": artifact.row_count,
        "is_truncated": bool(artifact.is_truncated),
    }


def _filters_without_prepared_name(filters):
    cleaned = dict(filters or {})
    cleaned.pop("prepared_report_name", None)
    return cleaned


def _rows_as_dicts(rows, columns):
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows

    normalized_columns = _normalize_columns(columns)
    fieldnames = [column.get("fieldname") for column in normalized_columns]
    converted = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            converted.append(row)
            continue
        converted.append({
            fieldname: row[index] if index < len(row) else None
            for index, fieldname in enumerate(fieldnames)
            if fieldname
        })
    return converted


def _is_waiting_for_prepared_report(report, raw_result, report_result):
    return (
        bool(report.prepared_report)
        and bool(raw_result.get("prepared_report"))
        and not raw_result.get("doc")
        and report_result.get("row_count", 0) == 0
    )


def _handle_missing_prepared_report(report_name, filters, permission_context, generate_if_missing):
    report_filters = _filters_without_prepared_name(filters)
    existing = _get_existing_pending_prepared_report(
        report_name,
        report_filters,
        permission_context.user,
    )
    if existing:
        return _prepared_report_pending_result(report_name, report_filters, existing, created=False)

    if not generate_if_missing:
        return {
            "report": report_name,
            "filters": report_filters,
            "row_count": 0,
            "result": [],
            "summary": {"row_count": 0},
            "prepared_report": True,
            "prepared_status": "Missing",
            "is_pending": True,
            "message": "No completed prepared report exists for these filters.",
        }

    prepared_report = _create_prepared_report(report_name, report_filters, permission_context.user)
    return _prepared_report_pending_result(report_name, report_filters, prepared_report, created=True)


def _get_existing_pending_prepared_report(report_name, filters, user):
    from frappe.core.doctype.prepared_report.prepared_report import process_filters_for_prepared_report

    prepared_filters = process_filters_for_prepared_report(filters)
    rows = frappe.get_all(
        "Prepared Report",
        filters={
            "report_name": report_name,
            "filters": prepared_filters,
            "owner": user,
            "status": ("in", ("Queued", "Started")),
        },
        fields=["name", "status", "creation"],
        order_by="creation desc",
        limit=1,
    )
    if not rows:
        return None
    return rows[0]


def _create_prepared_report(report_name, filters, user):
    from frappe.core.doctype.prepared_report.prepared_report import process_filters_for_prepared_report

    prepared_report = frappe.get_doc({
        "doctype": "Prepared Report",
        "report_name": report_name,
        "filters": process_filters_for_prepared_report(filters),
    })
    prepared_report.owner = user
    prepared_report.insert(ignore_permissions=True)
    frappe.db.commit()
    return prepared_report


def _prepared_report_pending_result(report_name, filters, prepared_report, created):
    name = prepared_report.get("name") if isinstance(prepared_report, dict) else prepared_report.name
    status = prepared_report.get("status") if isinstance(prepared_report, dict) else prepared_report.status
    return {
        "report": report_name,
        "filters": filters,
        "row_count": 0,
        "result": [],
        "summary": {"row_count": 0},
        "prepared_report": True,
        "prepared_report_name": name,
        "prepared_status": status or "Queued",
        "is_pending": True,
        "created_prepared_report": bool(created),
        "message": (
            "Prepared Report generation has been queued. "
            "Run reports.run again with the same filters or prepared_report_name after it completes."
        ),
    }


def _normalize_columns(columns):
    """Normalize Frappe report column metadata without depending on fixed field names."""
    normalized = []
    for index, column in enumerate(columns or []):
        if isinstance(column, dict):
            fieldname = column.get("fieldname") or column.get("field") or _safe_fieldname(column.get("label"))
            normalized.append({
                "fieldname": fieldname,
                "label": column.get("label") or fieldname,
                "fieldtype": column.get("fieldtype") or column.get("type"),
                "index": index,
            })
        elif isinstance(column, str):
            label, fieldtype = _parse_legacy_column(column)
            normalized.append({
                "fieldname": _safe_fieldname(label),
                "label": label,
                "fieldtype": fieldtype,
                "index": index,
            })
    return [column for column in normalized if column.get("fieldname")]


def _parse_legacy_column(column):
    label = column
    fieldtype = None
    if ":" in column:
        label, meta = column.split(":", 1)
        fieldtype = meta.split("/", 1)[0].strip() or None
    return label.strip(), fieldtype


def _safe_fieldname(label):
    if not label:
        return None
    return frappe.scrub(str(label).split(":", 1)[0])


def _summarize_report_rows(rows, columns=None):
    """Return a dynamic compact summary for query-report results."""
    if not isinstance(rows, list):
        return {}

    numeric_fields = _infer_numeric_fields(rows, columns)
    label_field = _infer_label_field(rows, numeric_fields, columns)
    selected_numeric_fields = numeric_fields[:12]

    return {
        "row_count": len(rows),
        "label_field": label_field,
        "numeric_fields": selected_numeric_fields,
        "numeric_stats": _build_numeric_stats(rows, selected_numeric_fields),
        "top_rows_by_numeric_field": _build_top_rows(rows, selected_numeric_fields[:6], label_field),
    }


def _infer_numeric_fields(rows, columns=None):
    column_order = {column["fieldname"]: column["index"] for column in columns or []}
    column_fieldtypes = {column["fieldname"]: column.get("fieldtype") for column in columns or []}
    numeric_column_fields = {
        column["fieldname"]
        for column in columns or []
        if column.get("fieldtype") in {"Currency", "Float", "Int", "Percent", "Duration"}
    }
    stats = {}
    row_count = len(rows)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field, value in row.items():
            if field == "idx" or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            field_stats = stats.setdefault(field, {"count": 0, "total_abs": 0, "values": []})
            field_stats["count"] += 1
            field_stats["total_abs"] += abs(value)
            field_stats["values"].append(value)

    minimum_count = min(2, row_count) if row_count else 0
    fields = [
        field for field, field_stats in stats.items()
        if field in numeric_column_fields or field_stats["count"] >= minimum_count
    ]
    fields = [
        field for field in fields
        if not _looks_like_structural_number(
            stats[field]["values"],
            column_fieldtypes.get(field),
            field in numeric_column_fields,
        )
    ]
    return sorted(
        fields,
        key=lambda field: (
            0 if field in numeric_column_fields else 1,
            column_order.get(field, 10_000),
            -stats[field]["total_abs"],
            field,
        ),
    )


def _looks_like_structural_number(values, fieldtype, is_numeric_column):
    if fieldtype in {"Currency", "Float", "Percent", "Duration"}:
        return False
    if not values:
        return True

    integer_values = [value for value in values if float(value).is_integer()]
    if len(integer_values) != len(values):
        return False

    unique_values = set(values)
    max_abs = max(abs(value) for value in values)
    unique_ratio = len(unique_values) / len(values)
    looks_categorical = len(unique_values) <= 5 and unique_ratio <= 0.2 and max_abs <= 10
    return looks_categorical and not (is_numeric_column and len(unique_values) > 2)


def _infer_label_field(rows, numeric_fields, columns=None):
    numeric_field_set = set(numeric_fields)
    column_order = {column["fieldname"]: column["index"] for column in columns or []}
    scores = []

    for field in _row_fields(rows):
        if field in numeric_field_set:
            continue
        values = [
            str(row.get(field)).strip()
            for row in rows
            if isinstance(row, dict) and row.get(field) not in (None, "")
        ]
        if not values:
            continue

        unique_ratio = len(set(values)) / len(values)
        average_length = sum(len(value) for value in values) / len(values)
        readable_length_score = 1 if 2 <= average_length <= 100 else 0
        score = (len(values), readable_length_score, unique_ratio, -column_order.get(field, 10_000))
        scores.append((score, field))

    if not scores:
        return None
    return max(scores)[1]


def _row_fields(rows):
    fields = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in row:
            if field not in seen:
                seen.add(field)
                fields.append(field)
    return fields


def _build_numeric_stats(rows, numeric_fields):
    stats = {}
    for field in numeric_fields:
        values = [
            row.get(field)
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get(field), (int, float))
            and not isinstance(row.get(field), bool)
        ]
        if not values:
            continue
        stats[field] = {
            "total": round(sum(values), 2),
            "average": round(sum(values) / len(values), 2),
            "min": round(min(values), 2),
            "max": round(max(values), 2),
            "non_zero_count": len([value for value in values if value]),
        }
    return stats


def _build_top_rows(rows, numeric_fields, label_field):
    top_rows = {}
    for field in numeric_fields:
        candidates = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
                continue
            candidates.append({
                "label": row.get(label_field) if label_field else None,
                "value": round(value, 2),
            })
        top_rows[field] = sorted(candidates, key=lambda row: abs(row["value"]), reverse=True)[:5]
    return top_rows


def _compact_rows(rows, summary, sample_limit):
    if not isinstance(rows, list):
        return []

    label_field = summary.get("label_field")
    numeric_fields = summary.get("numeric_fields") or []
    selected_fields = [field for field in [label_field] if field]
    selected_fields.extend(field for field in numeric_fields[:8] if field not in selected_fields)

    for field in _row_fields(rows):
        if len(selected_fields) >= 12:
            break
        if (
            field not in selected_fields
            and field not in numeric_fields
            and _field_has_text_values(rows, field)
        ):
            selected_fields.append(field)

    compacted = []
    for row in rows[:sample_limit]:
        if not isinstance(row, dict):
            compacted.append(row)
            continue
        compacted.append({
            field: row.get(field)
            for field in selected_fields
            if field in row and row.get(field) not in (None, "")
        })
    return compacted


def _field_has_text_values(rows, field):
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get(field)
        if value in (None, "") or isinstance(value, bool):
            continue
        return not isinstance(value, (int, float))
    return False
