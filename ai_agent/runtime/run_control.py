import frappe
from frappe import _
from frappe.utils import cint


ACTIVE_RUN_STATUSES = ("Queued", "Planning", "Running", "Resuming")
TERMINAL_RUN_STATUSES = ("Completed", "Failed", "Cancelled", "Timed Out")
RUN_JOB_PREFIX = "ai-agent-run-"


def run_job_id(run_name):
    return f"{RUN_JOB_PREFIX}{run_name}"


def run_timeout_seconds(agent=None, settings=None):
    settings = settings or frappe.get_doc("AI Settings")
    base_timeout = cint(settings.maximum_run_seconds) or 600
    if not agent:
        return base_timeout

    steps = cint(agent.maximum_steps)
    tools = cint(agent.maximum_tool_calls)
    if steps >= 25 or tools >= 50:
        return max(base_timeout, min(1800, max(steps * 30, tools * 15)))
    return base_timeout


def enqueue_run(
    run_name,
    user=None,
    method="ai_agent.runtime.run_control.execute_run_job",
    timeout=None,
    enqueue_after_commit=False,
):
    run = frappe.get_doc("AI Run", run_name)
    agent = frappe.get_doc("AI Agent", run.agent)
    timeout = cint(timeout) or run_timeout_seconds(agent=agent) + 120
    job_id = run_job_id(run_name)
    try:
        frappe.enqueue(
            method,
            queue="long",
            timeout=timeout,
            job_id=job_id,
            deduplicate=True,
            run_name=run_name,
            user=user or run.user,
            enqueue_after_commit=enqueue_after_commit,
        )
    except Exception:
        mark_interrupted_run(
            run_name,
            _("The agent request could not be added to the background queue. Please retry."),
        )
        raise
    return job_id


def execute_run_job(run_name, user=None):
    from ai_agent.runtime.orchestrator import execute_run

    run = frappe.get_doc("AI Run", run_name)
    if run.status in TERMINAL_RUN_STATUSES or run.cancel_requested:
        return

    # Never fall back to Administrator: that would bypass the entire
    # permission-intersection model the platform is built on.
    job_user = user or run.user
    if not job_user or job_user == "Guest":
        frappe.log_error(
            f"AI Run {run_name} has no valid owner user; execution refused.",
            "AI Agent Run Rejected",
        )
        mark_interrupted_run(run_name, _("Agent execution was rejected because the run has no owner user."))
        return
    frappe.set_user(job_user)

    try:
        execute_run(run_name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"AI Agent Run Failed: {run_name}")
        mark_interrupted_run(run_name, _("Agent background execution failed. Check the error log for details."))
        raise


def recover_run_if_orphaned(run, stale_after_seconds=None):
    """Close an active run only when its durable queue job is no longer active."""
    if run.status not in ACTIVE_RUN_STATUSES:
        return False

    settings = frappe.get_doc("AI Settings")
    request_timeout = cint(settings.request_timeout) or 30
    stale_after_seconds = cint(stale_after_seconds) or max(request_timeout * 2 + 30, 180)
    last_activity = run.modified or run.started_at or run.creation
    idle_seconds = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), last_activity)
    if idle_seconds < stale_after_seconds:
        return False

    try:
        from frappe.utils.background_jobs import get_job_status

        status = get_job_status(run_job_id(run.name))
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


def stop_run_job(run_name):
    """Stop an actively executing run job and return whether a stop signal was sent."""
    from frappe.utils.background_jobs import get_job, get_redis_conn
    from rq.command import send_stop_job_command

    job = get_job(run_job_id(run_name))
    if not job or not job.worker_name:
        return False
    send_stop_job_command(get_redis_conn(), job.id)
    return True


def mark_interrupted_run(run_name, message):
    run = frappe.get_doc("AI Run", run_name)
    if run.status in TERMINAL_RUN_STATUSES:
        return
    run.status = "Failed"
    run.error_summary = message
    run.completed_at = frappe.utils.now_datetime()
    run.save(ignore_permissions=True)
    frappe.db.commit()
    try:
        frappe.publish_realtime("ai_agent:run_failed", {"run_id": run_name}, user=run.user)
    except Exception:
        pass
