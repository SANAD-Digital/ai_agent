import frappe
from frappe import _
from ai_agent.runtime.skill_registry import SkillRegistry


def handle_list(arguments, permission_context):
    """Handle skills.list tool call."""
    registry = SkillRegistry()
    agent_name = permission_context.agent_name or arguments.get("agent")
    skills = registry.list_effective_skills(
        permission_context.user, agent_name, permission_context.company
    )
    return {"skills": skills}


def handle_load(arguments, permission_context):
    """Handle skills.load tool call."""
    skill_name = arguments.get("skill_name")
    if not skill_name:
        return {"error": "skill_name is required"}

    agent_name = permission_context.agent_name or arguments.get("agent")
    registry = SkillRegistry()
    try:
        skill = registry.load_skill(
            skill_name, permission_context.user, agent_name, permission_context.company
        )
        return skill
    except Exception as e:
        return {"error": str(e)}


def handle_read_reference(arguments, permission_context):
    """Handle skills.read_reference tool call."""
    skill_name = arguments.get("skill_name")
    reference = arguments.get("reference", "")
    if not skill_name:
        return {"error": "skill_name is required"}

    registry = SkillRegistry()
    try:
        skill = registry.load_skill(
            skill_name,
            permission_context.user,
            permission_context.agent_name,
            permission_context.company,
        )
        return {"skill": skill_name, "reference": skill.get("instructions", "")}
    except Exception as e:
        return {"error": str(e)}
