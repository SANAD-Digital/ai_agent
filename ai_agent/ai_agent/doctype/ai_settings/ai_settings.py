import frappe
from frappe import _
from frappe.model.document import Document


class AISettings(Document):
    def validate(self):
        seen = set()
        for row in self.get("access_rules") or []:
            if row.role and row.user:
                frappe.throw(_("الصف {0}: اختر دوراً أو مستخدماً، لا الاثنين معاً.").format(row.idx))
            key = (row.reference_doctype, row.reference_name, row.role or "", row.user or "")
            if key in seen:
                frappe.throw(_("الصف {0} مكرر.").format(row.idx))
            seen.add(key)

    def on_update(self):
        from ai_agent.runtime import access

        access.clear_cache()
