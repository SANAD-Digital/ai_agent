"""Give existing AI Provider Model rows their Model Type.

The column was added with default "Chat", so every row created before it is
Chat; speech, image, video, embedding and agent-only models are reclassified by
name. Keep this patch until every production site has migrated past it.
"""

import frappe


def execute():
	from ai_agent.ai_agent.doctype.ai_provider.ai_provider import model_type_of

	for row in frappe.get_all("AI Provider Model", fields=["name", "model_name", "model_type"]):
		kind = model_type_of(row.model_name)
		if kind != (row.model_type or "Chat"):
			frappe.db.set_value("AI Provider Model", row.name, "model_type", kind, update_modified=False)
