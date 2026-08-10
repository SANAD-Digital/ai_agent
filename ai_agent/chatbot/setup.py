from __future__ import annotations

import frappe

from ai_agent.chatbot import settings


SETTINGS_DOCTYPE = "AI Chatbot Settings"

CHATBOT_SKILLS = (
    "core-agent-behavior",
    "accounting-analysis",
    "receivables-analysis",
    "payables-analysis",
    "sales-analysis",
    "customer-analysis",
    "report-analysis",
    "data-quality-audit",
    "frappe-schema-discovery",
)


def ensure_chatbot_configuration():
    """Create the System Chatbot agent and default settings."""
    agent = ensure_chatbot_agent()
    ensure_settings(agent)
    frappe.db.commit()
    return agent


def ensure_chatbot_agent() -> str:
    name = settings.DEFAULT_AGENT_NAME
    existing = frappe.db.exists("AI Agent", name) or frappe.db.exists("AI Agent", {"agent_name": name})

    values = {
        "description": "System chatbot agent for technical support and problem solving inside the ERP.",
        "enabled": 1,
        "is_default": 0,
        "read_only": 1,
        "allow_actions": 0,
        "require_approval_for_all_writes": 1,
        "reasoning_effort": settings.reasoning_effort(),
        "maximum_steps": 25,
        "maximum_tool_calls": 50,
        "system_instructions": settings.system_prompt(),
    }

    model = settings.model() or (
        frappe.db.get_single_value("AI Settings", "default_model")
        if frappe.db.exists("DocType", "AI Settings")
        else None
    )
    if model:
        values["model"] = model

    if existing:
        agent = frappe.get_doc("AI Agent", existing)
        for field, value in values.items():
            if field == "system_instructions" and agent.system_instructions:
                continue
            agent.set(field, value)
        agent.save(ignore_permissions=True)
        ensure_chatbot_skill_grants(agent.name)
        return agent.name

    agent = frappe.get_doc({"doctype": "AI Agent", "agent_name": name, **values})
    agent.insert(ignore_permissions=True)
    ensure_chatbot_skill_grants(agent.name)
    return agent.name


def ensure_chatbot_skill_grants(agent: str):
    """Grant read/analysis skills needed for support and problem solving."""
    if any(not frappe.db.exists("AI Skill", skill) for skill in CHATBOT_SKILLS):
        try:
            from ai_agent.runtime.skill_registry import SkillRegistry

            SkillRegistry().sync_app_skills()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Chatbot Skill Sync Failed")

    for skill in CHATBOT_SKILLS:
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
        "enabled": 1,
        "agent": agent or frappe.db.exists("AI Agent", settings.DEFAULT_AGENT_NAME),
        "company": frappe.defaults.get_user_default("Company") or frappe.db.get_default("Company"),
        "enable_problem_solving": 1,
        "reasoning_effort": settings.DEFAULT_REASONING_EFFORT,
        "temperature": settings.DEFAULT_TEMPERATURE,
        "max_history_messages": 8,
        "widget_title": settings.DEFAULT_WIDGET_TITLE,
        "welcome_message": settings.DEFAULT_WELCOME_MESSAGE,
        "system_prompt": settings.DEFAULT_SYSTEM_PROMPT,
        "error_reply": settings.DEFAULT_ERROR_REPLY,
    }
    changed = False
    for field, value in defaults.items():
        if value is not None and doc.get(field) in (None, ""):
            doc.set(field, value)
            changed = True

    if changed:
        doc.save(ignore_permissions=True)
    return doc
