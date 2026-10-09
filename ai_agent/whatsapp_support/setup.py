from __future__ import annotations

import frappe

from ai_agent.whatsapp_support import settings


SETTINGS_DOCTYPE = "AI WhatsApp Support Settings"


def ensure_whatsapp_support_configuration():
    """Create the WhatsApp support AI Agent and default settings."""
    agent = ensure_support_agent()
    ensure_support_skill_grants(agent)
    ensure_settings(agent)
    frappe.db.commit()
    return agent


def ensure_support_agent() -> str:
    name = settings.DEFAULT_AGENT_NAME
    existing = frappe.db.exists("AI Agent", name) or frappe.db.exists("AI Agent", {"agent_name": name})

    values = {
        "description": "Business and technical support agent for incoming WhatsApp conversations.",
        "enabled": 1,
        "is_default": 0,
        "read_only": 1,
        "allow_actions": 0,
        "require_approval_for_all_writes": 1,
        # One support_user serves every WhatsApp customer: shared-identity
        # memory would leak one customer's data into another's replies.
        "disable_memory": 1,
        "reasoning_effort": "high",
        "maximum_steps": 30,
        "maximum_tool_calls": 60,
        "system_instructions": support_instructions(),
    }

    model = frappe.db.get_single_value("AI Settings", "default_model") if frappe.db.exists("DocType", "AI Settings") else None
    if model:
        values["model"] = model

    if existing:
        agent = frappe.get_doc("AI Agent", existing)
        for field, value in values.items():
            if field == "system_instructions" and agent.system_instructions:
                continue
            agent.set(field, value)
        agent.save(ignore_permissions=True)
        ensure_support_skill_grants(agent.name)
        return agent.name

    agent = frappe.get_doc({"doctype": "AI Agent", "agent_name": name, **values})
    agent.insert(ignore_permissions=True)
    ensure_support_skill_grants(agent.name)
    return agent.name


def ensure_support_skill_grants(agent: str):
    """Grant the WhatsApp support agent the read-only skills it needs for business replies."""
    required_skills = (
        "core-agent-behavior",
        "whatsapp-business-support",
        "sales",
        "accounting",
    )
    if any(not frappe.db.exists("AI Skill", skill) for skill in required_skills):
        try:
            from ai_agent.runtime.skill_registry import SkillRegistry

            SkillRegistry().sync_app_skills()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WhatsApp Support Skill Sync Failed")

    for skill in required_skills:
        if not frappe.db.exists("AI Skill", skill):
            continue
        exists = frappe.db.exists(
            "AI Skill Grant",
            {"agent": agent, "skill": skill, "grant_type": "All Users", "enabled": 1},
        )
        if exists:
            continue
        grant = frappe.get_doc(
            {
                "doctype": "AI Skill Grant",
                "agent": agent,
                "skill": skill,
                "grant_type": "All Users",
                "enabled": 1,
            }
        )
        grant.insert(ignore_permissions=True)


def ensure_settings(agent: str | None = None):
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return None

    doc = frappe.get_single(SETTINGS_DOCTYPE)
    defaults = {
        # No default support user: an admin must pick a dedicated low-privilege
        # user, and the integration stays off until that is done.
        "enabled": 1 if settings.support_user() else 0,
        "agent": agent or frappe.db.exists("AI Agent", settings.DEFAULT_AGENT_NAME),
        "whatsapp_device": _default_whatsapp_device(),
        "company": frappe.defaults.get_user_default("Company") or frappe.db.get_default("Company"),
        "allow_group_chats": 0,
        "create_issue": 1,
        "issue_doctype": "Issue" if frappe.db.exists("DocType", "Issue") else None,
        "max_history_messages": 6,
        "error_reply": settings.DEFAULT_REPLY_ON_ERROR,
    }
    changed = False
    for field, value in defaults.items():
        if value is not None and doc.get(field) in (None, ""):
            doc.set(field, value)
            changed = True

    if changed:
        problem = settings.support_user_problem(doc.support_user)
        if problem and doc.support_user:
            # Runtime already refuses an unsafe support user; don't fail migrate over it.
            frappe.log_error(problem, "WhatsApp AI Support Refused")
            doc.flags.ignore_validate = True
        doc.save(ignore_permissions=True)
    return doc


def support_instructions() -> str:
    return (
        "أنت وكيل واتساب للأعمال. استخدم المهارات الممنوحة لك لتحليل طلب العميل والرد عليه من بيانات ERP المتاحة."
    )


def _default_whatsapp_device() -> str | None:
    if not frappe.db.exists("DocType", "WhatsApp Device"):
        return None
    return frappe.db.get_value("WhatsApp Device", {"default": 1}, "name") or frappe.db.get_value(
        "WhatsApp Device",
        {},
        "name",
    )
