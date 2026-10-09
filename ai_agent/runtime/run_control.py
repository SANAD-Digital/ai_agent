import frappe
from frappe import _
from frappe.utils import cint


ACTIVE_RUN_STATUSES = ("Queued", "Planning", "Running", "Resuming")
TERMINAL_RUN_STATUSES = ("Completed", "Failed", "Cancelled", "Timed Out")
RUN_JOB_PREFIX = "ai-agent-run-"


def run_job_id(run_name):
    return f"{RUN_JOB_PREFIX}{run_name}"


def active_job_id(run):
    """A run produced by the chat engine lives in the message's job, not the run's."""
    if run.get("message"):
        from ai_agent.engine.runner import job_id

        return job_id(run.message)
    return run_job_id(run.name)


def run_timeout_seconds(agent=None, settings=None):
    settings = settings or frappe.get_cached_doc("AI Settings")
    base_timeout = cint(settings.maximum_run_seconds) or 600
    if not agent:
        return base_timeout

    steps = cint(agent.maximum_steps)
    tools = cint(agent.maximum_tool_calls)
    if steps >= 25 or tools >= 50:
        return max(base_timeout, min(1800, max(steps * 30, tools * 15)))
    return base_timeout


def recover_run_if_orphaned(run, stale_after_seconds=None):
    """Close an active run only when its durable queue job is no longer active."""
    if run.status not in ACTIVE_RUN_STATUSES:
        return False

    settings = frappe.get_cached_doc("AI Settings")
    request_timeout = cint(settings.request_timeout) or 30
    stale_after_seconds = cint(stale_after_seconds) or max(request_timeout * 2 + 30, 180)
    last_activity = run.modified or run.started_at or run.creation
    idle_seconds = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), last_activity)
    if idle_seconds < stale_after_seconds:
        return False

    try:
        from frappe.utils.background_jobs import get_job_status

        status = get_job_status(active_job_id(run))
        status_value = str(getattr(status, "value", status) or "").lower()
    except Exception:
        # Redis being unavailable is not proof that execution was lost.
        return False

    if status_value in {"queued", "started", "deferred", "scheduled"}:
        return False

    mark_interrupted_run(
        run.name,
        _("Agent execution was interrupted because its background worker stopped. Please retry the request."),
    )
    run.reload()
    return True


def recover_orphaned_runs():
    cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-3)
    rows = frappe.get_all(
        "AI Run",
        filters={"status": ["in", ACTIVE_RUN_STATUSES], "modified": ["<", cutoff]},
        fields=["name"],
        page_length=200,
    )
    recovered = 0
    for row in rows:
        try:
            recovered += int(recover_run_if_orphaned(frappe.get_doc("AI Run", row.name)))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Run Recovery Failed: {row.name}")
    return recovered


def mark_interrupted_run(run_name, message):
    run = frappe.get_doc("AI Run", run_name)
    if run.status in TERMINAL_RUN_STATUSES:
        return
    run.status = "Failed"
    run.error_summary = message
    run.completed_at = frappe.utils.now_datetime()
    # The run's session/message may have been deleted since it was queued; a
    # broken link must not keep it "active" forever (recovery would retry it
    # every five minutes and log an error each time).
    run.flags.ignore_links = True
    run.save(ignore_permissions=True)
    frappe.db.commit()
    if run.get("message") and frappe.db.exists("AI Chat Message", run.message):
        from ai_agent.engine.runner import fail_orphaned_message

        fail_orphaned_message(run.message, message)
    try:
        frappe.publish_realtime("ai_agent:run_failed", {"run_id": run_name}, user=run.user)
    except Exception:
        # Best-effort: realtime UI events are cosmetic and must never fail a run.
        pass
