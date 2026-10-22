"""Install and migrate hooks.

App content ships in Frappe's own places and is synced by Frappe:
- the AI workspace, ``desktop_icon/`` and ``workspace_sidebar/``;
- the access roles: created from the DocType permissions that name them;
- standard agents, plugins and preset providers: ``ai_agent/ai_agent/{ai_agent,ai_plugin,ai_provider}/``
  (``importable_doctypes`` in hooks.py; ``standard_records.keep_site_values`` keeps what the site owns);
- settings defaults: the DocType JSON.

Only what is derived from code is synced here: the tool name options, the skills and their grants, the support
chatbot agent (its model and prompt come from its settings) and the default metrics.
"""

import frappe

SINGLES_WITH_DEFAULTS = ("AI Settings", "Gemini OCR Settings")


def after_install():
    sync()


def after_migrate():
    sync()


def sync():
    from ai_agent.ai_agent.doctype.ai_skill_grant.ai_skill_grant import grant_standard_skills
    from ai_agent.chatbot.setup import ensure_chatbot_configuration
    from ai_agent.metrics.defaults import ensure_default_metrics
    from ai_agent.runtime.skill_registry import SkillRegistry

    store_single_defaults()
    # before the skill sync: a skill row naming a tool missing from AI Skill Tool's options is rejected
    sync_tool_name_options()
    SkillRegistry().sync_app_skills()
    grant_standard_skills()
    ensure_chatbot_configuration()
    ensure_default_metrics()
    frappe.db.commit()


def store_single_defaults():
    """A settings DocType never saved reads every field as empty through get_single_value: save it once, with
    the defaults from its JSON."""
    for doctype in SINGLES_WITH_DEFAULTS:
        if not frappe.db.get_singles_dict(doctype):
            doc = frappe.get_single(doctype)
            doc.flags.ignore_validate = True
            doc.flags.ignore_mandatory = True
            doc.save(ignore_permissions=True)


def sync_tool_name_options():
    """Keep AI Skill Tool options aligned with the registered tool names."""
    from ai_agent.runtime.tool_registry import ToolRegistry

    options = "\n".join(sorted(tool["name"] for tool in ToolRegistry().list_tools()))
    field_name = frappe.db.exists("DocField", {"parent": "AI Skill Tool", "fieldname": "tool_name"})
    if field_name:
        frappe.db.set_value("DocField", field_name, "options", options, update_modified=False)
        # the cached meta still holds the old options; without this the skill sync rejects new tools
        frappe.clear_cache(doctype="AI Skill Tool")
