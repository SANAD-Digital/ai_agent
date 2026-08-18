import frappe


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
        try:
            db_modified = frappe.db.get_value("AI Run Step", step.name, "modified")
            if db_modified:
                step.modified = db_modified
            step.flags.ignore_version = True
            step.save(ignore_permissions=True, ignore_version=True)
        except Exception:
            values = {
                "status": status,
                "completed_at": step.completed_at,
                "duration_ms": step.duration_ms,
            }
            if summary:
                values["summary"] = summary
            frappe.db.set_value("AI Run Step", step.name, values, update_modified=False)
        frappe.db.commit()
        self._publish_event("ai_agent:step_added", step.run)

    def update_run_status(self, run_name, status, **kwargs):
        """Update the run status and optional fields."""
        values = {"status": status}
        for key, val in kwargs.items():
            values[key] = val
        frappe.db.set_value("AI Run", run_name, values)
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
            # Best-effort: realtime UI events are cosmetic and must never fail a run.
            pass
