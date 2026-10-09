from frappe.model.document import Document


class AIMetricComposite(Document):
	def validate(self):
		if self.composite_type == "formula":
			from ai_agent.metrics.engine import evaluate_formula

			aliases = {(row.alias or row.metric): 1 for row in self.items}
			evaluate_formula(self.formula, aliases)

	def on_update(self):
		from ai_agent.metrics.catalog import clear_cache

		clear_cache()

	def on_trash(self):
		self.on_update()
