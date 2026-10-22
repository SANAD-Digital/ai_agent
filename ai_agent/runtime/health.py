"""How the assistant is doing: the numbers behind the "AI Agent Health" report and the hourly alert.

Read-only aggregates over AI Chat Message, AI Tool Call, AI Approval Request and the scheduler log.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, now_datetime

# Alert when, in the last hour, at least this many replies failed and they are at least this share of all.
ALERT_MIN_FAILED = 3
ALERT_FAILED_SHARE = 0.25
STUCK_MINUTES = 15


def daily(days=14):
    """One row per day: replies, failures, tool calls and their failures, approvals, tokens, cost."""
    since = add_to_date(now_datetime(), days=-cint(days or 14))
    msgs = frappe.db.sql(
        """select date(creation) day, count(*) replies,
            sum(status = 'failed') failed, sum(status = 'cancelled') cancelled,
            sum(status = 'completed' and ifnull(text, '') = '') empty,
            sum(ifnull(input_tokens, 0) + ifnull(cache_read_input_tokens, 0) + ifnull(cache_creation_input_tokens, 0)) input_tokens,
            sum(ifnull(output_tokens, 0)) output_tokens, sum(ifnull(estimated_cost, 0)) cost,
            avg(nullif(duration_ms, 0)) / 1000 avg_seconds
        from `tabAI Chat Message` where role = 'assistant' and creation >= %s group by date(creation)""",
        since, as_dict=True)
    tools = frappe.db.sql(
        """select date(creation) day, count(*) tool_calls, sum(status = 'Failed') tool_failed, sum(status = 'Denied') tool_denied
        from `tabAI Tool Call` where creation >= %s group by date(creation)""", since, as_dict=True)
    approvals = frappe.db.sql(
        """select date(creation) day, count(*) approvals, sum(status = 'Executed') approvals_executed,
            sum(status = 'Failed') approvals_failed, sum(status = 'Expired') approvals_expired
        from `tabAI Approval Request` where creation >= %s group by date(creation)""", since, as_dict=True)
    rows = {}
    for source in (msgs, tools, approvals):
        for r in source:
            rows.setdefault(r.day, frappe._dict(day=r.day)).update({k: v for k, v in r.items() if k != "day"})
    out = []
    for day in sorted(rows, reverse=True):
        r = rows[day]
        r.failed_share = flt(cint(r.get("failed")) / cint(r.get("replies")) * 100, 1) if cint(r.get("replies")) else 0
        out.append(r)
    return out


def stuck_messages():
    """Replies still pending/streaming long after they started, or waiting for an approval that is no longer pending."""
    cutoff = add_to_date(now_datetime(), minutes=-STUCK_MINUTES)
    return frappe.db.sql(
        """select name, session, status, modified from `tabAI Chat Message`
        where role = 'assistant' and status in ('pending', 'streaming') and modified < %s
        order by modified desc limit 50""", cutoff, as_dict=True)


def failed_jobs(hours=24):
    since = add_to_date(now_datetime(), hours=-hours)
    # Scheduled Job Type names are hashes: match the app by the job's method.
    return frappe.db.sql(
        """select t.method scheduled_job_type, count(*) n, max(l.creation) last
        from `tabScheduled Job Log` l join `tabScheduled Job Type` t on t.name = l.scheduled_job_type
        where l.status = 'Failed' and l.creation >= %s and t.method like 'ai_agent.%%'
        group by t.method""", since, as_dict=True)


def check_and_alert():
    """Hourly: tell System Managers (bell notification) when replies fail, replies hang, or an ai_agent
    scheduled job fails. Each kind of alert is sent at most once per 6 hours."""
    since = add_to_date(now_datetime(), hours=-1)
    counts = frappe.db.sql(
        """select count(*) total, sum(status = 'failed') failed from `tabAI Chat Message`
        where role = 'assistant' and creation >= %s""", since, as_dict=True)[0]
    problems = []
    failed, total = cint(counts.failed), cint(counts.total)
    if failed >= ALERT_MIN_FAILED and failed / max(total, 1) >= ALERT_FAILED_SHARE:
        problems.append(("failures", _("{0} of {1} assistant replies failed in the last hour.").format(failed, total)))
    stuck = stuck_messages()
    if stuck:
        problems.append(("stuck", _("{0} assistant replies have been running for more than {1} minutes.").format(len(stuck), STUCK_MINUTES)))
    for job in failed_jobs(hours=1):
        problems.append((f"job:{job.scheduled_job_type}", _("Scheduled job {0} failed {1} times in the last hour.").format(job.scheduled_job_type, job.n)))
    for key, text in problems:
        _notify(key, text)
    return problems


def _notify(key, text):
    cache_key = f"ai_agent:health_alert:{key}"
    if frappe.cache.get_value(cache_key):
        return
    frappe.cache.set_value(cache_key, 1, expires_in_sec=6 * 3600)
    users = frappe.get_all("Has Role", filters={"role": "System Manager", "parenttype": "User", "parent": ("not in", ("Administrator", "Guest"))}, pluck="parent")
    users = [u for u in set(users) if frappe.db.get_value("User", u, "enabled")] or ["Administrator"]
    for user in users:
        frappe.get_doc({
            "doctype": "Notification Log",
            "for_user": user,
            "type": "Alert",
            "subject": _("AI assistant: {0}").format(text),
            "email_content": text + "<br>" + _("Open the AI Agent Health report for details."),
            "document_type": "Report",
            "document_name": "AI Agent Health",
        }).insert(ignore_permissions=True)
