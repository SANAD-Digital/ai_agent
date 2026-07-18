import frappe
from frappe import _


def after_install():
    """Setup after app installation."""
    ensure_standard_configuration()


def after_migrate():
    """Keep app-managed baseline data available after migrations."""
    ensure_standard_configuration()


def ensure_standard_configuration():
    """Install or refresh app-managed defaults without storing secrets."""
    create_default_settings()
    create_default_agents()
    sync_tool_name_options()
    sync_skills()
    create_default_skill_grants()
    frappe.db.commit()


def create_default_settings():
    """Create default AI Settings if not exists."""
    if not frappe.db.exists("AI Settings", "AI Settings"):
        settings = frappe.get_doc({
            "doctype": "AI Settings",
            "enabled": 0,
            "provider_type": "OpenAI",
            "default_model": "gpt-4o",
            "request_timeout": 120,
            "maximum_run_seconds": 300,
            "maximum_steps": 12,
            "maximum_tool_calls": 25,
            "maximum_rows_per_query": 500,
            "maximum_result_bytes": 102400,
            "store_full_tool_results": 0,
            "enable_memory": 1,
            "memory_retention_days": 90,
            "default_approval_ttl_minutes": 1440,
        })
        settings.insert(ignore_permissions=True)
        return

    settings = frappe.get_doc("AI Settings", "AI Settings")
    defaults = {
        "request_timeout": 120,
        "maximum_run_seconds": 300,
        "maximum_steps": 12,
        "maximum_tool_calls": 25,
        "maximum_rows_per_query": 500,
        "maximum_result_bytes": 102400,
        "store_full_tool_results": 0,
        "enable_memory": 1,
        "memory_retention_days": 90,
        "default_approval_ttl_minutes": 1440,
    }
    changed = False
    for field, value in defaults.items():
        if settings.get(field) in (None, ""):
            settings.set(field, value)
            changed = True
    if changed:
        settings.save(ignore_permissions=True)


def create_default_agents():
    """Create or refresh the standard app agents."""
    for agent_name, values in _standard_agents().items():
        existing = _get_agent_docname(agent_name)
        if existing:
            agent = frappe.get_doc("AI Agent", existing)
            for field, value in values.items():
                if field in {"enabled", "is_default"}:
                    if agent.get(field) in (None, ""):
                        agent.set(field, value)
                else:
                    agent.set(field, value)
            agent.save(ignore_permissions=True)
            continue

        agent = frappe.get_doc({
            "doctype": "AI Agent",
            "agent_name": agent_name,
            **values,
        })
        agent.insert(ignore_permissions=True)


def sync_skills():
    """Sync skills from app files."""
    from ai_agent.runtime.skill_registry import SkillRegistry
    registry = SkillRegistry()
    registry.sync_app_skills()


def sync_tool_name_options():
    """Keep AI Skill Tool options aligned with the registered tool names."""
    from ai_agent.runtime.tool_registry import ToolRegistry

    tool_names = sorted(tool["name"] for tool in ToolRegistry().list_tools())
    options = "\n".join(tool_names)
    field_name = frappe.db.exists(
        "DocField",
        {"parent": "AI Skill Tool", "fieldname": "tool_name"},
    )
    if field_name:
        frappe.db.set_value("DocField", field_name, "options", options, update_modified=False)


def create_default_skill_grants():
    """Grant app skills to the standard agents."""
    skills = frappe.get_all(
        "AI Skill",
        filters={"enabled": 1, "source_type": "App File"},
        pluck="name",
    )
    if not skills:
        return

    for agent_name in _standard_agents():
        agent_docname = _get_agent_docname(agent_name)
        if not agent_docname:
            continue
        allowed_skills = _skills_for_agent(agent_name, skills)
        for skill in allowed_skills:
            if _global_skill_grant_exists(agent_docname, skill):
                continue
            grant = frappe.get_doc({
                "doctype": "AI Skill Grant",
                "agent": agent_docname,
                "skill": skill,
                "grant_type": "All Users",
                "company": None,
                "enabled": 1,
            })
            grant.insert(ignore_permissions=True)
            frappe.db.set_value("AI Skill Grant", grant.name, "company", None, update_modified=False)


def _standard_agents():
    return {
        "ERP Analyst": {
            "description": "Read-only data analysis agent for ERPNext",
            "enabled": 1,
            "is_default": 1,
            "read_only": 1,
            "allow_actions": 0,
            "require_approval_for_all_writes": 1,
            "reasoning_effort": "medium",
            "system_instructions": (
                "You are an ERP data analyst. Analyze data and provide insights. "
                "Do not perform write operations. Ask one concise clarification only when required."
            ),
        },
        "ERP Operations Agent": {
            "description": "Operations agent that can prepare and request approved document changes",
            "enabled": 1,
            "is_default": 0,
            "read_only": 0,
            "allow_actions": 1,
            "require_approval_for_all_writes": 1,
            "reasoning_effort": "medium",
            "system_instructions": (
                "You are an ERP operations agent. You can prepare documents and request changes, "
                "but all write operations require user approval. When multiple missing records must be created, "
                "group them into one bulk approval instead of requesting approval one record at a time."
            ),
        },
        "ERP Power Operator": {
            "description": "High-capacity operations agent for large compound ERP tasks and multi-document workflows",
            "enabled": 1,
            "is_default": 0,
            "read_only": 0,
            "allow_actions": 1,
            "require_approval_for_all_writes": 1,
            "reasoning_effort": "high",
            "maximum_steps": 60,
            "maximum_tool_calls": 120,
            "system_instructions": (
                "You are a high-capacity ERP operations agent for difficult compound work. "
                "Handle large documents and multi-document chains by discovering schemas dynamically, "
                "creating missing prerequisite records through approval, validating target payloads, "
                "and keeping the original objective active across approvals and follow-up continuations. "
                "Never invent master data or transaction values. Never perform writes without approval. "
                "When several prerequisite records are missing, request one bulk create approval for the full set. "
                "Prefer one complete target payload for large child tables instead of one action per row. "
                "If you must stop after a prerequisite approval, clearly state what remains and offer to continue."
            ),
        },
    }


def _get_agent_docname(agent_name):
    return (
        frappe.db.exists("AI Agent", agent_name)
        or frappe.db.exists("AI Agent", {"agent_name": agent_name})
    )


def _global_skill_grant_exists(agent, skill):
    return bool(frappe.db.sql(
        """
        select name
        from `tabAI Skill Grant`
        where agent = %s
          and skill = %s
          and grant_type = 'All Users'
          and ifnull(company, '') = ''
        limit 1
        """,
        (agent, skill),
    ))


def _skills_for_agent(agent, skills):
    if agent == "ERP Analyst":
        excluded = {"document-creation", "document-workflow"}
        return [skill for skill in skills if skill not in excluded]
    return list(skills)
