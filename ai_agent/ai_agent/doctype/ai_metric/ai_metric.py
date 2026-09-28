import json

import frappe
from frappe.model.document import Document


class AIMetric(Document):
	def validate(self):
		if self.base_filters:
			try:
				json.loads(self.base_filters)
			except ValueError:
				frappe.throw(frappe._("Base Filters must be valid JSON."))
		from ai_agent.metrics.engine import _field_exists

		for fieldname in (self.date_field, self.time_field, self.company_field):
			if fieldname and not _field_exists(self.source_doctype, fieldname):
				frappe.throw(frappe._("Field {0} does not exist on {1}.").format(fieldname, self.source_doctype))
		from ai_agent.metrics.resolvers import DIMENSION_RESOLVERS, METRIC_RESOLVERS

		if self.resolver and self.resolver not in METRIC_RESOLVERS:
			frappe.throw(frappe._("Unknown metric resolver {0}.").format(self.resolver))
		for row in self.dimensions:
			if row.resolver and row.resolver not in DIMENSION_RESOLVERS:
				frappe.throw(frappe._("Unknown dimension resolver {0}.").format(row.resolver))

	def on_update(self):
		from ai_agent.metrics.catalog import clear_cache

		clear_cache()

	def on_trash(self):
		self.on_update()
