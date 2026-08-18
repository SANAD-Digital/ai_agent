import json
import frappe


MAX_ARTIFACT_ROWS = 10000


class ArtifactService:
    """Creates and manages artifacts for AI Runs."""

    def create_table(self, run_name, title, rows, columns=None, sequence=1, total_row_count=None, summary=None):
        """Create a table artifact."""
        original_count = total_row_count if total_row_count is not None else len(rows)
        if len(rows) > MAX_ARTIFACT_ROWS:
            rows = rows[:MAX_ARTIFACT_ROWS]
            is_truncated = 1
        else:
            is_truncated = 0

        content = {
            "columns": columns or (list(rows[0].keys()) if rows else []),
            "rows": rows,
        }

        artifact = frappe.get_doc({
            "doctype": "AI Artifact",
            "parent": run_name,
            "parenttype": "AI Run",
            "parentfield": "artifacts",
            "run": run_name,
            "artifact_type": "Table",
            "title": title,
            "content_json": json.dumps(content, default=str),
            "summary": summary or "",
            "row_count": original_count,
            "is_truncated": is_truncated or (original_count > len(rows)),
            "sequence": sequence,
        })
        artifact.insert(ignore_permissions=True)
        frappe.db.commit()
        return artifact

    def create_chart(self, run_name, title, chart_spec, sequence=1):
        """Create a chart artifact (spec only, no JS generation)."""
        artifact = frappe.get_doc({
            "doctype": "AI Artifact",
            "parent": run_name,
            "parenttype": "AI Run",
            "parentfield": "artifacts",
            "run": run_name,
            "artifact_type": "Chart",
            "title": title,
            "content_json": json.dumps(chart_spec, default=str),
            "sequence": sequence,
        })
        artifact.insert(ignore_permissions=True)
        frappe.db.commit()
        return artifact

    def create_document_links(self, run_name, title, links, sequence=1):
        """Create a document links artifact."""
        artifact = frappe.get_doc({
            "doctype": "AI Artifact",
            "parent": run_name,
            "parenttype": "AI Run",
            "parentfield": "artifacts",
            "run": run_name,
            "artifact_type": "Document Links",
            "title": title,
            "content_json": json.dumps(links, default=str),
            "row_count": len(links),
            "sequence": sequence,
        })
        artifact.insert(ignore_permissions=True)
        frappe.db.commit()
        return artifact

    def create_text(self, run_name, title, text, sequence=1):
        """Create a text artifact."""
        artifact = frappe.get_doc({
            "doctype": "AI Artifact",
            "parent": run_name,
            "parenttype": "AI Run",
            "parentfield": "artifacts",
            "run": run_name,
            "artifact_type": "Text",
            "title": title,
            "content_json": json.dumps({"text": text}, default=str),
            "sequence": sequence,
        })
        artifact.insert(ignore_permissions=True)
        frappe.db.commit()
        return artifact
