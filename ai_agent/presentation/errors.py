"""Errors as the user should read them: what happened, why, and what to do next. Never a raw tool error."""

import re

RULES = [
    # (pattern, kind, title, reason template, suggestion, retry). English sources, translated in friendly();
    # the reason template takes {0} = target, {1} = permission (only the permission rule uses it).
    (r"No (read|create|write|submit|cancel|delete) permission on (?:DocType )?'?([^'.]+)'?", "permission", "You do not have the required permission",
     "Your account does not have {1} to “{0}”.", "Ask your system manager to grant this permission, then try again.", False),
    (r"fiscal year", "validation", "Fiscal year not set",
     "The report could not run because the fiscal year or period is not set.", "Choose a fiscal year or a start and end date, then try again.", True),
    (r"not (found or not )?accessible|does not exist|not found", "not_found", "Document not available",
     "The requested document does not exist, or you do not have permission to view it.", "Check the document number, or search for it by name.", False),
    (r"missing required argument\(s\): (.+?)\.?$|([a-z_, ]+) (?:is|are) required", "validation", "Missing information",
     "The request is missing: {0}.", "Fill in the missing information, then try again.", True),
    (r"high cost|requires additional filters|add more filters", "validation", "Too many results",
     "The query is too broad to run as it is.", "Narrow it down by period, company or party.", True),
    (r"call limit", "limit", "Limit reached", "This tool has been used the maximum number of times for this task.", "Start a new request.", False),
    (r"rate limit", "limit", "Too many requests", "You have exceeded the number of requests allowed per minute.", "Wait a minute, then try again.", True),
    (r"system configuration; it cannot be created or changed", "policy", "System setting the assistant cannot change",
     "“{0}” is a system setting and can only be changed in ERPNext.", "Open the setting in ERPNext, or ask your system manager.", False),
    (r"not allowed by this agent|is not available|requires approval", "policy", "Action not available",
     "This action is not enabled in the assistant's current settings.", "Ask your system manager to enable it if you need it.", False),
    (r"Tool execution failed|Internal server error", "failure", "An unexpected error occurred",
     "The operation could not be completed because of an internal error, which has been logged for the system manager.",
     "Try again, and if the error persists, tell your system manager.", True),
]
VERBS = {"read": "read access", "create": "create access", "write": "edit access", "submit": "submit access",
         "cancel": "cancel access", "delete": "delete access"}
ARABIC = re.compile(r"[\u0600-\u06FF]")


def friendly(message, tool=None):
    """{kind, title, reason, suggestion, retry, raw} for any error text."""
    from ai_agent.presentation.i18n import t
    from ai_agent.presentation.labels import doctype_label

    raw = str(message or "").strip()
    for pattern, kind, title, reason, suggestion, retry in RULES:
        match = re.search(pattern, raw, re.IGNORECASE)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        target = groups[-1].strip() if groups else ""
        verb = t(VERBS[groups[0].lower()]) if kind == "permission" and groups and groups[0].lower() in VERBS else ""
        if kind in ("permission", "policy") and target:
            target = doctype_label(target) if target[:1].isupper() else target
        if kind == "policy" and "{0}" in reason and not target:
            target = raw.split(" is system configuration")[0].strip()
        return {"kind": kind, "title": t(title), "reason": t(reason, target or "—", verb), "suggestion": t(suggestion), "retry": retry, "raw": raw}
    if ARABIC.search(raw):      # already written for the user
        return {"kind": "failure", "title": t("The request could not be completed"), "reason": raw, "suggestion": None, "retry": False, "raw": raw}
    return {"kind": "failure", "title": t("The request could not be completed"), "reason": t("The operation did not finish."),
            "suggestion": t("Try again, and if the error persists, tell your system manager."),
            "retry": True, "raw": raw}


def text_of(view):
    return " ".join(x for x in (view["title"] + ":", view["reason"], view.get("suggestion")) if x)
