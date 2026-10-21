import frappe
from frappe.utils import flt, get_url

from ai_agent.runtime.artifact_service import ArtifactService

CHART_TYPES = {"bar", "line", "pie", "percentage", "donut"}


def _run_name(arguments, permission_context):
    # The gateway stamps the executing run on the permission context; the
    # model never knows (and must not control) the run id itself.
    return getattr(permission_context, "run_name", None) or arguments.get("run")


def handle_create_table(arguments, permission_context):
    """Handle artifacts.create_table tool call."""
    run_name = _run_name(arguments, permission_context)
    title = arguments.get("title", "Table")
    rows = arguments.get("rows", [])
    columns = arguments.get("columns")

    if not run_name:
        return {"error": "run is required"}

    service = ArtifactService()
    artifact = service.create_table(run_name, title, rows, columns)
    return {"artifact_name": artifact.name, "title": title, "row_count": len(rows)}


def handle_create_chart(arguments, permission_context):
    """Handle artifacts.create_chart tool call."""
    run_name = _run_name(arguments, permission_context)
    title = arguments.get("title", "Chart")
    chart_spec = arguments.get("chart_spec", {})

    if not run_name:
        return {"error": "run is required"}

    chart_spec, error = _normalize_chart_spec(chart_spec)
    if error:
        return {"error": error}

    service = ArtifactService()
    artifact = service.create_chart(run_name, title, chart_spec)
    # Clients that cannot draw the chart (e.g. MCP) can open this page.
    chart_url = get_url(f"/ai-chart?artifact={frappe.utils.quote(artifact.name)}")
    return {"artifact_name": artifact.name, "title": title, "chart_url": chart_url}


def _plain(value):
    """Labels are text: tags are removed here, and the chart renderers escape what is left."""
    return frappe.utils.strip_html(str(value))[:200]


def _normalize_chart_spec(spec):
    """Reject malformed specs instead of storing a chart that renders empty."""
    shape = "chart_spec must be {type, labels: [..], datasets: [{name, values: [..]}]}"
    if not isinstance(spec, dict):
        return None, shape
    labels = spec.get("labels")
    datasets = spec.get("datasets")
    if not isinstance(labels, list) or not labels or not all(isinstance(label, (str, int, float)) for label in labels):
        return None, f"labels must be a flat array of strings. {shape}"
    if not isinstance(datasets, list) or not datasets:
        return None, f"datasets must be a non-empty array. {shape}"

    normalized = []
    for dataset in datasets:
        values = dataset.get("values") if isinstance(dataset, dict) else None
        if not isinstance(values, list) or len(values) != len(labels):
            return None, f"each dataset needs a flat values array with one number per label. {shape}"
        if not all(isinstance(v, (int, float, str)) for v in values):
            return None, f"values must be numbers. {shape}"
        normalized.append({"name": _plain(dataset.get("name") or ""), "values": [flt(v) for v in values]})

    chart_type = str(spec.get("type") or "bar")
    return {
        "type": chart_type if chart_type in CHART_TYPES else "bar",
        "labels": [_plain(label) for label in labels],
        "datasets": normalized,
    }, None


def handle_create_document_links(arguments, permission_context):
    """Handle artifacts.create_document_links tool call."""
    run_name = _run_name(arguments, permission_context)
    title = arguments.get("title", "Document Links")
    links = arguments.get("links", [])

    if not run_name:
        return {"error": "run is required"}

    service = ArtifactService()
    artifact = service.create_document_links(run_name, title, links)
    return {"artifact_name": artifact.name, "title": title, "count": len(links)}
