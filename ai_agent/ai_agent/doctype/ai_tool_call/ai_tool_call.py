import frappe
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now


class AIToolCall(Document):
    @staticmethod
    def clear_old_logs(days=90):
        """Log Settings retention (hooks.default_log_clearing_doctypes)."""
        table = frappe.qb.DocType("AI Tool Call")
        frappe.db.delete(table, filters=(table.creation < (Now() - Interval(days=days))))
