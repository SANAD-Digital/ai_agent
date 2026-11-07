"""AI Chatbot Assignment: the support window (bubble) of one user, with the settings of AI Chatbot Settings at the
user's level. chatbot/settings.py reads the user's value first and the site's when the field is empty."""

import frappe
from frappe import _
from frappe.model.document import Document


class AIChatbotAssignment(Document):
	def __setup__(self):
		# Frappe fills an empty Company with the creator's default; empty here means the site's company
		self.dont_update_if_missing = ["company"]

	def validate(self):
		from ai_agent.engine.providers.factory import validate_provider_model

		validate_provider_model(self.provider, self.model)
		if self.agent and not frappe.db.get_value("AI Agent", self.agent, "enabled"):
			frappe.throw(_("Agent {0} is disabled.").format(self.agent))
		if self.company and not frappe.has_permission("Company", "read", self.company, user=self.user):
			frappe.throw(_("{0} cannot read company {1}.").format(self.user, self.company))
