import frappe
from frappe.utils import cint
from frappe.model.document import Document


class AISkillGrant(Document):
    def validate(self):
        if self.grant_type == "Role" and not self.role:
            frappe.throw("Role is required when grant type is Role.")
        if self.grant_type == "User" and not self.user:
            frappe.throw("User is required when grant type is User.")
        if self.valid_from and self.valid_until and self.valid_from > self.valid_until:
            frappe.throw("Valid From cannot be after Valid Until.")


# The agents shipped in ai_agent/ai_agent/ai_agent/ and the app skills each one gets.
STANDARD_AGENTS = ("ERP Analyst", "ERP Operations Agent", "ERP Power Operator", "Implementation")
# Skills granted by role, never to "All Users": they change the system itself.
ROLE_GRANTED_SKILLS = {"customization": "System Manager"}
# The read-only agent gets no document playbook and no customization.
READ_ONLY_EXCLUDED = {"documents", "customization"}


def grant_standard_skills():
    """Grant the app's skills to the standard agents (on migrate, after the skill sync). A grant an admin
    disabled stays disabled; customization belongs to agents that allow it."""
    skills = frappe.get_all("AI Skill", filters={"enabled": 1, "source_type": "App File"}, pluck="name")
    if not skills:
        return
    for agent in STANDARD_AGENTS:
        if not frappe.db.exists("AI Agent", agent):
            continue
        allowed = _skills_for_agent(agent, skills)
        if "customization" not in allowed:
            for grant in frappe.get_all("AI Skill Grant", filters={"agent": agent, "skill": "customization", "enabled": 1}, pluck="name"):
                frappe.db.set_value("AI Skill Grant", grant, "enabled", 0)
        for skill in allowed:
            if skill in ROLE_GRANTED_SKILLS:
                _ensure_role_grant(agent, skill, ROLE_GRANTED_SKILLS[skill])
            elif not frappe.db.exists("AI Skill Grant", {"agent": agent, "skill": skill, "grant_type": "All Users", "company": ("is", "not set")}):
                frappe.get_doc({"doctype": "AI Skill Grant", "agent": agent, "skill": skill, "grant_type": "All Users", "enabled": 1}).insert(ignore_permissions=True)


def _skills_for_agent(agent, skills):
    implicit = set(frappe.get_all("AI Skill", filters={"name": ("in", skills), "allow_implicit_invocation": 1}, pluck="name"))
    skills = [s for s in skills if s == "core-agent-behavior" or s not in implicit]
    if cint(frappe.db.get_value("AI Agent", agent, "read_only")):
        return [s for s in skills if s not in READ_ONLY_EXCLUDED]
    if not cint(frappe.db.get_value("AI Agent", agent, "allow_customization")):
        return [s for s in skills if s != "customization"]
    return skills


def _ensure_role_grant(agent, skill, role):
    if not frappe.db.exists("Role", role):
        return
    if frappe.db.exists("AI Skill Grant", {"agent": agent, "skill": skill, "grant_type": "Role", "role": role}):
        return
    frappe.get_doc({"doctype": "AI Skill Grant", "agent": agent, "skill": skill, "grant_type": "Role", "role": role, "enabled": 1}).insert(ignore_permissions=True)
