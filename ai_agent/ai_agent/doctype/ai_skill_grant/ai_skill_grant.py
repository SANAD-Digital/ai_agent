import frappe
from frappe.model.document import Document


class AISkillGrant(Document):
    def validate(self):
        if self.grant_type == "Role" and not self.role:
            frappe.throw("Role is required when grant type is Role.")
        if self.grant_type == "User" and not self.user:
            frappe.throw("User is required when grant type is User.")
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            frappe.throw("Valid From cannot be after Valid Until.")
