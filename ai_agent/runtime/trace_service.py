import frappe
from frappe import _


class TraceService:
    """Manages run steps and activity timeline for AI Runs."""

    def add_step(self, run_name, sequence, step_type, title, summary="", status="Started", is_user_visible=1):
        """Add a step to the run timeline."""
        step = frappe.get_doc({
            "doctype": "AI Run Step",
            "run": run_name,
            "parent": run_name,
            "parenttype": "AI Run",
            "parentfield": "steps",
            "sequence": sequence,
            "step_type": step_type,
            "title": title,
            "summary": summary,
            "status": status,
            "is_user_visible": is_user_visible,
            "started_at": frappe.utils.now_datetime(),
        })
        step.insert(ignore_permissions=True)
        frappe.db.commit()
        self._publish_event("ai_agent:step_added", run_name)
        return step

    def complete_step(self, step_name, status="Completed", summary=None):
        """Mark a step as completed."""
        step = frappe.get_doc("AI Run Step", step_name)
        step.status = status
        step.completed_at = frappe.utils.now_datetime()
        if step.started_at:
            delta = step.completed_at - step.started_at
            step.duration_ms = int(delta.total_seconds() * 1000)
        if summary:
            step.summary = summary
        step.save(ignore_permissions=True)
        frappe.db.commit()
        self._publish_event("ai_agent:step_added", step.run)

    def update_run_status(self, run_name, status, **kwargs):
        """Update the run status and optional fields."""
        run = frappe.get_doc("AI Run", run_name)
        run.status = status
        for key, val in kwargs.items():
            if hasattr(run, key):
                setattr(run, key, val)
        run.save(ignore_permissions=True)
        frappe.db.commit()
        self._publish_event("ai_agent:run_updated", run_name)

    def publish_realtime(self, event_type, run_name, user=None):
        """Publish a realtime event."""
        self._publish_event(event_type, run_name, user)

    def _publish_event(self, event, run_name, user=None):
        try:
            frappe.publish_realtime(
                event,
                {"run_id": run_name},
                user=user,
            )
        except Exception:
            pass
