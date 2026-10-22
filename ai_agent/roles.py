"""The app's access roles, one per surface. System Manager always has access.

AI Chat User     the full chat page (agent-workspace)        «مساعد (شات كاملة)»
AI Chatbot User  the floating support window on every page   «مساعد (استخدام الفقاعة)»
AI MCP User      external AI tools over MCP                  «مساعد (وكيل خارجي)»
"""

import frappe
from frappe import _

CHAT_ROLE = "AI Chat User"
BUBBLE_ROLE = "AI Chatbot User"
MCP_ROLE = "AI MCP User"
ADMIN_ROLE = "System Manager"
APP_ROLES = (CHAT_ROLE, BUBBLE_ROLE, MCP_ROLE)


def has_role(roles, user=None):
	user = user or frappe.session.user
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & (set(roles) | {ADMIN_ROLE}))


def assert_role(roles, user=None):
	if not has_role(roles, user):
		frappe.throw(_("You do not have access to the assistant here. Ask your administrator for the role."), frappe.PermissionError)
