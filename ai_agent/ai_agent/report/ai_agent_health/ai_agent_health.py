import frappe
from frappe import _

from ai_agent.runtime import health


def execute(filters=None):
    filters = filters or {}
    columns = [
        {"fieldname": "day", "label": _("Day"), "fieldtype": "Date", "width": 110},
        {"fieldname": "replies", "label": _("Replies"), "fieldtype": "Int", "width": 90},
        {"fieldname": "failed", "label": _("Failed"), "fieldtype": "Int", "width": 80},
        {"fieldname": "failed_share", "label": _("Failed %"), "fieldtype": "Percent", "width": 90},
        {"fieldname": "empty", "label": _("Empty replies"), "fieldtype": "Int", "width": 110},
        {"fieldname": "cancelled", "label": _("Stopped"), "fieldtype": "Int", "width": 80},
        {"fieldname": "avg_seconds", "label": _("Avg seconds"), "fieldtype": "Float", "precision": 1, "width": 100},
        {"fieldname": "tool_calls", "label": _("Tool calls"), "fieldtype": "Int", "width": 100},
        {"fieldname": "tool_failed", "label": _("Tool failures"), "fieldtype": "Int", "width": 110},
        {"fieldname": "tool_denied", "label": _("Tool denials"), "fieldtype": "Int", "width": 110},
        {"fieldname": "approvals", "label": _("Approvals"), "fieldtype": "Int", "width": 90},
        {"fieldname": "approvals_failed", "label": _("Approvals failed"), "fieldtype": "Int", "width": 130},
        {"fieldname": "approvals_expired", "label": _("Approvals expired"), "fieldtype": "Int", "width": 130},
        {"fieldname": "input_tokens", "label": _("Input tokens"), "fieldtype": "Int", "width": 120},
        {"fieldname": "output_tokens", "label": _("Output tokens"), "fieldtype": "Int", "width": 120},
        {"fieldname": "cost", "label": _("Estimated cost (USD)"), "fieldtype": "Float", "precision": 2, "width": 150},
    ]
    data = health.daily(filters.get("days") or 14)
    stuck = health.stuck_messages()
    jobs = health.failed_jobs()
    message = []
    if stuck:
        message.append(_("{0} replies are stuck (running more than {1} minutes).").format(len(stuck), health.STUCK_MINUTES))
    for job in jobs:
        message.append(_("Scheduled job {0} failed {1} times in the last 24 hours.").format(job.scheduled_job_type, job.n))
    chart = {
        "data": {
            "labels": [str(r.day) for r in reversed(data)],
            "datasets": [
                {"name": _("Replies"), "values": [r.get("replies") or 0 for r in reversed(data)]},
                {"name": _("Failed"), "values": [r.get("failed") or 0 for r in reversed(data)]},
            ],
        },
        "type": "bar",
    }
    return columns, data, "<br>".join(message) or None, chart
