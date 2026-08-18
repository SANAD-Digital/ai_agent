import re

import frappe


def execute():
    if not frappe.db.has_column("AI Memory", "company"):
        return

    rows = frappe.get_all(
        "AI Memory",
        filters={"company": ("is", "not set")},
        fields=["name", "content", "source_run"],
    )
    for row in rows:
        company = _company_from_run(row.source_run) or _company_from_content(row.content)
        if company and frappe.db.exists("Company", company):
            frappe.db.set_value("AI Memory", row.name, "company", company, update_modified=False)


def _company_from_run(run_name):
    if not run_name or not frappe.db.exists("AI Run", run_name):
        return None
    return frappe.db.get_value("AI Run", run_name, "company")


def _company_from_content(content):
    match = re.search(r"(?:^|;\s*)company=([^;]+)", str(content or ""))
    return match.group(1).strip() if match else None
