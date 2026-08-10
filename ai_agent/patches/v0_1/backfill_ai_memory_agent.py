import re

import frappe


def execute():
    if not frappe.db.has_column("AI Memory", "agent"):
        return

    rows = frappe.get_all(
        "AI Memory",
        filters={"scope": "Agent", "agent": ["is", "not set"]},
        fields=["name", "content"],
        limit=10000,
    )
    for row in rows:
        agent = _extract_agent(row.get("content"))
        if agent and frappe.db.exists("AI Agent", agent):
            frappe.db.set_value("AI Memory", row.name, "agent", agent, update_modified=False)


def _extract_agent(content):
    match = re.search(r"(?:^|[;\n])\s*agent\s*=\s*([^;\n]+)", content or "", re.I)
    return match.group(1).strip() if match else ""
