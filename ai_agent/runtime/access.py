"""Who may use which skill and which plugin: the rules table in AI Settings.

Everything is denied until a rule allows it. A rule names a skill or a plugin
and either a user, a role, or neither (= everyone). The most specific matching
level decides: the user's own rows, then rows for any of the user's roles,
then rows for everyone. Within the deciding level a deny (Allow unticked)
beats an allow. Administrator is always allowed, as everywhere in Frappe.

Skills that run on their own (allow_implicit_invocation, e.g. the agent's
core behaviour) are policy, not something a user picks, so they are not
subject to these rules.
"""

from __future__ import annotations

import frappe

SKILL = "AI Skill"
PLUGIN = "AI Plugin"
CACHE_KEY = "ai_agent:access_rules"


def clear_cache():
    frappe.cache.delete_value(CACHE_KEY)
    frappe.cache.delete_value("ai_agent:plugin_tools")


def _rules():
    rules = frappe.cache.get_value(CACHE_KEY)
    if rules is None:
        rules = [
            {"doctype": r.reference_doctype, "name": r.reference_name, "role": r.role or "", "user": r.user or "", "allow": int(r.allow or 0)}
            for r in frappe.get_all(
                "AI Access Rule",
                filters={"parenttype": "AI Settings", "parent": "AI Settings"},
                fields=["reference_doctype", "reference_name", "role", "user", "allow"],
                order_by="idx asc",
            )
        ] if frappe.db.exists("DocType", "AI Access Rule") else []
        frappe.cache.set_value(CACHE_KEY, rules, expires_in_sec=3600)
    return rules


def allowed_names(doctype, user=None):
    """The skill or plugin names `user` may use (a set; None means every one: Administrator)."""
    user = user or frappe.session.user
    if user == "Administrator":
        return None
    roles = set(frappe.get_roles(user))
    decision = {}   # name -> (level, allowed); a higher level is more specific
    for rule in _rules():
        if rule["doctype"] != doctype:
            continue
        if rule["user"]:
            if rule["user"] != user:
                continue
            level = 3
        elif rule["role"]:
            if rule["role"] not in roles:
                continue
            level = 2
        else:
            level = 1
        current = decision.get(rule["name"])
        if current is None or level > current[0]:
            decision[rule["name"]] = (level, bool(rule["allow"]))
        elif level == current[0] and not rule["allow"]:
            decision[rule["name"]] = (level, False)
    return {name for name, (_, ok) in decision.items() if ok}


def is_allowed(doctype, name, user=None):
    names = allowed_names(doctype, user)
    return names is None or name in names


def plugin_tools():
    """{tool name: plugin name} for every enabled plugin."""
    mapping = frappe.cache.get_value("ai_agent:plugin_tools")
    if mapping is None:
        mapping = {}
        if frappe.db.exists("DocType", PLUGIN):
            for row in frappe.get_all(PLUGIN, filters={"enabled": 1}, fields=["name", "tool"]):
                mapping.setdefault(row.tool, []).append(row.name)
        frappe.cache.set_value("ai_agent:plugin_tools", mapping, expires_in_sec=3600)
    return mapping


def tool_allowed(tool_name, user=None):
    """A tool that belongs to a plugin is usable only by users allowed that plugin."""
    plugins = plugin_tools().get(tool_name)
    if not plugins:
        return True
    names = allowed_names(PLUGIN, user)
    return names is None or any(p in names for p in plugins)


def allowed_plugins(user=None):
    """Enabled plugins `user` may pick from the / menu."""
    if not frappe.db.exists("DocType", PLUGIN):
        return []
    names = allowed_names(PLUGIN, user)
    rows = frappe.get_all(PLUGIN, filters={"enabled": 1}, fields=["name", "title", "description", "icon", "output", "tool", "instructions"], order_by="title asc")
    return [r for r in rows if names is None or r.name in names]
