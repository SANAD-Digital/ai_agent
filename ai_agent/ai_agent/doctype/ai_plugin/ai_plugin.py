import frappe
from frappe import _
from frappe.model.document import Document


class AIPlugin(Document):
    def validate(self):
        from ai_agent.runtime.tool_registry import ToolRegistry

        if not ToolRegistry().get_tool(self.tool):
            frappe.throw(_("الأداة {0} غير موجودة في سجل الأدوات.").format(self.tool))

    def on_update(self):
        from ai_agent.runtime import access

        access.clear_cache()

    def on_trash(self):
        from ai_agent.runtime import access

        access.clear_cache()
