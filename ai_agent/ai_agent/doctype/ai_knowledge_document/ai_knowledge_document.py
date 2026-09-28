import frappe
from frappe.model.document import Document


class AIKnowledgeDocument(Document):
	def validate(self):
		if self.is_new() or self.has_value_changed("file"):
			self.indexing_status = "Pending"

	def on_update(self):
		if self.indexing_status == "Pending" and self.file:
			frappe.enqueue(
				"ai_agent.knowledge.indexer.index_document",
				queue="long",
				timeout=1200,
				document=self.name,
				enqueue_after_commit=True,
			)

	def on_trash(self):
		frappe.db.delete("AI Knowledge Chunk", {"document": self.name})
