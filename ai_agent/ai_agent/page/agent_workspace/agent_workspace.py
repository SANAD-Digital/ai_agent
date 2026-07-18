import frappe


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/app"
        raise frappe.Redirect
    context.title = "Agent Workspace"
    return context
