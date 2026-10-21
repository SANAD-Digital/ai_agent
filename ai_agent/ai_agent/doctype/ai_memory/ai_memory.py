import frappe
from frappe import _
from frappe.model.document import Document


def memory_title(content):
    """The start of the content, without the old "company=…; agent=…;" tags."""
    import re

    text = re.sub(r"^(\s*(agent|company)=[^;]+;\s*)+", "", " ".join(str(content or "").split()))
    return text if len(text) <= 90 else text[:89].rstrip() + "…"


class AIMemory(Document):
    def validate(self):
        self.title = memory_title(self.content)
        if self.scope != "Agent" and not self.user:
            frappe.throw(_("User is required unless Scope is Agent."))
        if self.scope == "Agent" and not self.agent:
            frappe.throw(_("Agent is required when Scope is Agent."))


def on_doctype_update():
	# Memory is always read per user (permission query, recall, cleanup).
	frappe.db.add_index("AI Memory", ["user"])
