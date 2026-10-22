import frappe
from frappe.model.document import Document


class AIAgent(Document):
    @staticmethod
    def prepare_for_import(docdict):
        """A standard agent (ai_agent/ai_agent/ai_agent/*.json) keeps the site's switch, provider and model."""
        from ai_agent.standard_records import keep_site_values

        keep_site_values(docdict, ("enabled", "is_default"))

    def validate(self):
        from ai_agent.engine.providers.factory import validate_provider_model

        validate_provider_model(self.provider, self.model)
        if self.is_default:
            existing = frappe.get_all(
                "AI Agent",
                filters={"is_default": 1, "name": ("!=", self.name), "enabled": 1},
                limit=1,
            )
            if existing:
                frappe.throw("Only one default agent can be enabled at a time.")