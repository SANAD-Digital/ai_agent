"""Skills for the loop: every granted skill is advertised (name + description);
always-on skills are inlined; the model loads the rest with skills.load."""

from __future__ import annotations

import frappe

from ai_agent.runtime.skill_registry import SkillRegistry


def granted(ctx):
    registry = SkillRegistry()
    try:
        return registry.list_effective_skills(ctx.user, ctx.agent_name, ctx.company) or []
    except Exception:
        frappe.log_error(frappe.get_traceback(), "AI skills: list_effective_skills")
        return []


def split(skills, ctx):
    """(implicit with instructions, advertised metadata)."""
    registry = SkillRegistry()
    implicit, advertised = [], []
    for meta in skills:
        name = meta.get("name")
        if meta.get("allow_implicit_invocation"):
            try:
                full = registry.load_skill(name, ctx.user, ctx.agent_name, ctx.company)
                if full.get("instructions"):
                    implicit.append({"name": name, "title": full.get("title") or name, "instructions": full["instructions"].strip()})
                    continue
            except Exception:
                # Its instructions are lost from the prompt: still advertised, so loading it on demand reports the error.
                frappe.log_error(frappe.get_traceback(), f"AI skills: implicit skill {name}")
        advertised.append({"name": name, "description": meta.get("description") or meta.get("title") or ""})
    return implicit, advertised


def preload(executor, skill_name):
    """Load a skill the user picked with /skill before the first model call.
    Returns the SKILL block or None."""
    from ai_agent.engine.types import Block, TOOL_USE

    fake_call = Block(kind=TOOL_USE, id="preload_skill", name="skills__load", data={"skill_name": skill_name})
    outcome = executor.execute(fake_call)
    for extra in outcome.extras:
        if extra.kind == "skill":
            return extra
    return None


def preload_plugin(executor, plugin_name):
    """The plugin the user picked with /: its instructions as a SKILL block, its tool enabled.
    Returns the block, or None when the plugin is missing, disabled or not allowed."""
    import frappe

    from ai_agent.engine.tools import plugin_text
    from ai_agent.engine.types import SKILL, Block
    from ai_agent.runtime import access

    plugin = next((p for p in access.allowed_plugins(executor.ctx.user) if p.name == plugin_name), None)
    if not plugin:
        frappe.log_error(f"plugin {plugin_name} not available to {executor.ctx.user}", "AI plugin preload")
        return None
    return Block(kind=SKILL, name=f"plugin:{plugin.name}", text=plugin_text(plugin), data={
        "title": plugin.title, "description": plugin.description, "plugin": 1, "icon": plugin.icon, "tools": [plugin.tool],
    })
