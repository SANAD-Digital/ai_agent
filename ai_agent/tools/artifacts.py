from ai_agent.runtime.artifact_service import ArtifactService


def handle_create_table(arguments, permission_context):
    """Handle artifacts.create_table tool call."""
    run_name = arguments.get("run")
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
    run_name = arguments.get("run")
    title = arguments.get("title", "Chart")
    chart_spec = arguments.get("chart_spec", {})

    if not run_name:
        return {"error": "run is required"}

    service = ArtifactService()
    artifact = service.create_chart(run_name, title, chart_spec)
    return {"artifact_name": artifact.name, "title": title}


def handle_create_document_links(arguments, permission_context):
    """Handle artifacts.create_document_links tool call."""
    run_name = arguments.get("run")
    title = arguments.get("title", "Document Links")
    links = arguments.get("links", [])

    if not run_name:
        return {"error": "run is required"}

    service = ArtifactService()
    artifact = service.create_document_links(run_name, title, links)
    return {"artifact_name": artifact.name, "title": title, "count": len(links)}
