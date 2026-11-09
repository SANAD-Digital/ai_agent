"""Records the app no longer ships. Frappe keeps them on an updated site: their tables and a form whose controller is
gone. Only the app's own records are removed (module AI Agent, not custom); a fresh install has none of them."""

import frappe

RETIRED = {
	# children after their parents
	"DocType": (
		"AI Chatbot Conversation", "AI Chatbot Message",  # the old support bubble (2026-10-10)
		"Financial Attachment Session", "Financial Attachment Session File", "Financial Attachment Session Page",
		"AI Model Capability", "AI Run Step",  # dead fields (2026-10-22)
		"AI WhatsApp Request", "AI WhatsApp Support Settings",  # WhatsApp left the app (2026-10-22)
	),
	"Page": ("purchase-invoice-ai",),
	"Workspace": ("AI Agent",),  # now "AI"
}


def execute():
	for doctype, names in RETIRED.items():
		for name in names:
			row = frappe.db.get_value(doctype, name, ["module", "custom"] if doctype == "DocType" else ["module"], as_dict=True)
			if not row or row.module != "AI Agent" or row.get("custom"):
				continue  # gone already, or a record of the site's own with the same name
			frappe.delete_doc(doctype, name, force=True, ignore_permissions=True, ignore_missing=True, delete_permanently=True)
