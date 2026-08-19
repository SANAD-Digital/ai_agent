"""Compact tool results before they enter the model context.

Pure functions extracted from the orchestrator: they shape what the model
sees (bounded rows, payload summaries instead of full payloads) without
touching run state.
"""

import hashlib
import json

from frappe.utils import cint


def summarize_payload(payload):
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}

    scalar_fields = []
    child_tables = []
    total_child_rows = 0
    nested_objects = []

    for field, value in payload.items():
        if isinstance(value, list):
            row_count = len(value)
            total_child_rows += row_count
            sample_keys = []
            for row in value[:1]:
                if isinstance(row, dict):
                    sample_keys = list(row.keys())[:20]
            child_tables.append({
                "field": field,
                "rows": row_count,
                "sample_fields": sample_keys,
            })
        elif isinstance(value, dict):
            nested_objects.append({"field": field, "fields": list(value.keys())[:20]})
        else:
            scalar_fields.append(field)

    payload_json = json.dumps(payload, sort_keys=True, default=str)
    return {
        "field_count": len(payload),
        "scalar_fields": scalar_fields[:40],
        "child_tables": child_tables,
        "nested_objects": nested_objects,
        "total_child_rows": total_child_rows,
        "estimated_bytes": len(payload_json.encode()),
        "is_large": len(payload_json) > 4000 or total_child_rows > 50,
    }


def compact_tool_result_for_model(tool_name, result):
    if not isinstance(result, dict):
        return result

    if tool_name == "reports.run":
        return {
            "report": result.get("report"),
            "filters": result.get("filters"),
            "row_count": result.get("row_count"),
            "summary": result.get("summary"),
            "sample_rows": (result.get("result") or [])[:20],
            "is_truncated": result.get("is_truncated"),
            "payload_mode": result.get("payload_mode"),
            "prepared_report": result.get("prepared_report"),
            "prepared_report_name": result.get("prepared_report_name"),
            "prepared_status": result.get("prepared_status"),
            "is_pending": result.get("is_pending"),
            "created_prepared_report": result.get("created_prepared_report"),
            "message": result.get("message"),
            "model_note": "Use summary first; request raw rows only if the user explicitly asks for row-level detail.",
        }

    if tool_name == "data.query" and "rows" in result:
        rows = result.get("rows") or []
        return {
            "row_count": result.get("row_count", len(rows)),
            "rows": rows[:50],
            "truncated": result.get("truncated") or len(rows) > 50,
            "model_note": "Rows are capped for context efficiency; use aggregation or narrower filters for more detail.",
        }

    if tool_name.startswith("actions."):
        compact = {
            key: result.get(key)
            for key in (
                "doctype", "valid", "errors", "missing_fields", "approval_required",
                "approval_id", "approval_status", "action_summary",
            )
            if key in result
        }
        payload = result.get("payload")
        if isinstance(payload, dict):
            compact["payload_summary"] = summarize_payload(payload)
            compact["payload_hash"] = hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode()
            ).hexdigest()
            compact["payload_omitted_for_context"] = True
        return compact or result

    return result


def tool_result_content_for_model(tool_registry, tool_name, result):
    """Apply the registered tool's model-context limit."""
    content = json.dumps(result, default=str, ensure_ascii=False)
    tool = tool_registry.get_tool(tool_name) or {}
    limit = cint(tool.get("model_result_max_chars")) or 4000
    return content[:limit]
