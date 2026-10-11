from frappe.model.document import Document


class AIChatbotSettings(Document):
    def validate(self):
        from ai_agent.engine.providers.factory import validate_provider_model

        validate_provider_model(self.provider, self.model)
