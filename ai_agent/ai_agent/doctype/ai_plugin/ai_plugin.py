import frappe
from frappe import _
from frappe.model.document import Document


class AIPlugin(Document):
    @staticmethod
    def prepare_for_import(docdict):
        """A standard plugin (ai_agent/ai_agent/ai_plugin/*.json) stays disabled where the admin disabled it."""
        from ai_agent.standard_records import keep_site_values

        keep_site_values(docdict, ("enabled",))

    def validate(self):
        from ai_agent.runtime.tool_registry import ToolRegistry

        if not ToolRegistry().get_tool(self.tool):
            frappe.throw(_("Tool {0} is not registered.").format(self.tool))

    def on_update(self):
        from ai_agent.runtime import access

        access.clear_cache()

    def on_trash(self):
        from ai_agent.runtime import access

        access.clear_cache()
