import json

import frappe
from frappe.utils import cint


def after_install():
    """Setup after app installation."""
    ensure_standard_configuration()


def after_migrate():
    """Keep app-managed baseline data available after migrations."""
    ensure_standard_configuration()


def ensure_standard_configuration():
    """Install or refresh app-managed defaults without storing secrets."""
    create_default_settings()
    create_default_gemini_ocr_settings()
    ensure_default_provider()
    create_default_agents()
    ensure_whatsapp_support_configuration()
    ensure_chatbot_configuration()
    ensure_workspace_sidebar()
    ensure_workspace_sidebar_doc()
    ensure_desktop_icon()
    sync_tool_name_options()
    sync_skills()
    create_default_skill_grants()
    ensure_purchase_invoice_agent_configuration()
    frappe.db.commit()


def ensure_workspace_sidebar():
    """Keep the app workspace visible and linked to the shipped pages."""
    workspace_name = _ensure_ai_workspace_name()
    if not workspace_name:
        return

    workspace = frappe.get_doc("Workspace", workspace_name)
    workspace.label = "AI"
    workspace.title = "AI"
    workspace.module = "AI Agent"
    workspace.is_hidden = 0
    workspace.public = 1
    workspace.icon = workspace.icon or "bot"

    _ensure_workspace_content_shortcut(workspace, "Purchase Invoice AI")
    _ensure_workspace_link(
        workspace,
        label="Purchase Invoice AI",
        link_type="Page",
        link_to="purchase-invoice-ai",
        after_label="Agent Workspace",
    )
    _ensure_workspace_shortcut(
        workspace,
        label="Purchase Invoice AI",
        link_to="purchase-invoice-ai",
        color="Red",
        after_label="Agent Workspace",
    )
    _ensure_workspace_link(
        workspace,
        label="Gemini OCR Settings",
        link_type="DocType",
        link_to="Gemini OCR Settings",
        after_label="AI Settings",
    )
    _ensure_workspace_link(
        workspace,
        label="AI Chatbot Settings",
        link_type="DocType",
        link_to="AI Chatbot Settings",
        after_label="Gemini OCR Settings",
    )
    _ensure_workspace_link(
        workspace,
        label="AI Chatbot Conversations",
        link_type="DocType",
        link_to="AI Chatbot Conversation",
        after_label="AI Chatbot Settings",
    )
    _refresh_workspace_card_counts(workspace)
    workspace.save(ignore_permissions=True)


def _ensure_ai_workspace_name():
    if frappe.db.exists("Workspace", "AI"):
        return "AI"
    if not frappe.db.exists("Workspace", "AI Agent"):
        return None
    return frappe.rename_doc(
        "Workspace",
        "AI Agent",
        "AI",
        force=True,
        show_alert=False,
        rebuild_search=False,
    )


def ensure_desktop_icon():
    """Expose the app workspace in the desk sidebar as AI."""
    if not frappe.db.exists("Workspace", "AI"):
        return

    icon_name = frappe.db.exists("Desktop Icon", "AI") or frappe.db.exists(
        "Desktop Icon",
        {"link_type": "Workspace Sidebar", "link_to": "AI"},
    )
    values = {
        "label": "AI",
        "icon_type": "Link",
        "link_type": "Workspace Sidebar",
        "link_to": "AI",
        "sidebar": "AI",
        "standard": 1,
        "app": "ai_agent",
        "hidden": 0,
        "logo_url": "/assets/ai_agent/desktop_icons/ai.svg",
    }

    if icon_name:
        icon = frappe.get_doc("Desktop Icon", icon_name)
        for field, value in values.items():
            icon.set(field, value)
        icon.save(ignore_permissions=True)
        return

    frappe.get_doc({"doctype": "Desktop Icon", **values}).insert(ignore_permissions=True)


def _ensure_workspace_content_shortcut(workspace, shortcut_name):
    try:
        content = json.loads(workspace.content or "[]")
    except Exception:
        return

    if any(
        block.get("type") == "shortcut"
        and block.get("data", {}).get("shortcut_name") == shortcut_name
        for block in content
    ):
        return

    block = {
        "id": "ai-agent-shortcut-purchase-invoice-ai",
        "type": "shortcut",
        "data": {"shortcut_name": shortcut_name, "col": 3},
    }
    insert_at = _content_index_after_shortcut(content, "Agent Workspace")
    content.insert(insert_at, block)
    workspace.content = json.dumps(content, separators=(",", ":"))


def _content_index_after_shortcut(content, shortcut_name):
    for index, block in enumerate(content):
        if (
            block.get("type") == "shortcut"
            and block.get("data", {}).get("shortcut_name") == shortcut_name
        ):
            return index + 1
    return len(content)


def _ensure_workspace_link(workspace, label, link_type, link_to, after_label=None):
    for row in workspace.links:
        if row.get("type") == "Link" and row.get("link_type") == link_type and row.get("link_to") == link_to:
            row.label = label
            row.hidden = 0
            return

    row = {
        "type": "Link",
        "label": label,
        "link_type": link_type,
        "link_to": link_to,
        "hidden": 0,
        "is_query_report": 0,
        "onboard": 0,
        "link_count": 0,
    }
    _insert_child_row(workspace, "links", row, _child_index_after_label(workspace.links, after_label))


def _ensure_workspace_shortcut(workspace, label, link_to, color, after_label=None):
    for row in workspace.shortcuts:
        if row.get("type") == "Page" and row.get("link_to") == link_to:
            row.label = label
            row.color = color
            return

    row = {"type": "Page", "label": label, "link_to": link_to, "color": color}
    _insert_child_row(workspace, "shortcuts", row, _child_index_after_label(workspace.shortcuts, after_label))


def _insert_child_row(doc, table_field, row, insert_at):
    rows = doc.get(table_field)
    child = doc.append(table_field, row)
    rows.remove(child)
    rows.insert(min(insert_at, len(rows)), child)
    for index, existing in enumerate(rows, start=1):
        existing.idx = index


def _child_index_after_label(rows, label):
    if not label:
        return len(rows)
    for index, row in enumerate(rows):
        if row.get("label") == label:
            return index + 1
    return len(rows)


def _refresh_workspace_card_counts(workspace):
    current_card = None
    counts = {}
    for row in workspace.links:
        if row.get("type") == "Card Break":
            current_card = row
            counts[current_card.name] = 0
            continue
        if current_card and row.get("type") == "Link" and not row.get("hidden"):
            counts[current_card.name] += 1

    for row in workspace.links:
        if row.get("type") == "Card Break":
            row.link_count = counts.get(row.name, row.link_count or 0)


def ensure_workspace_sidebar_doc():
    """Create the desk sidebar group for the AI app."""
    values = {
        "title": "AI",
        "header_icon": "bot",
        "module": "AI Agent",
        "standard": 1,
        "app": "ai_agent",
    }
    sidebar = (
        frappe.get_doc("Workspace Sidebar", "AI")
        if frappe.db.exists("Workspace Sidebar", "AI")
        else frappe.get_doc({"doctype": "Workspace Sidebar", **values})
    )
    for field, value in values.items():
        sidebar.set(field, value)
    sidebar.set("items", [])
    for item in _ai_workspace_sidebar_items():
        sidebar.append("items", item)

    if sidebar.is_new():
        sidebar.insert(ignore_permissions=True)
    else:
        sidebar.save(ignore_permissions=True)

    for old_name in ("AI Agent", "AI Agent Workspace"):
        if frappe.db.exists("Workspace Sidebar", old_name):
            frappe.delete_doc("Workspace Sidebar", old_name, ignore_permissions=True, force=True)


def _ai_workspace_sidebar_items():
    return [
        {"label": "AI", "type": "Link", "link_type": "Workspace", "link_to": "AI", "icon": "bot"},
        {
            "label": "Agent Workspace",
            "type": "Link",
            "link_type": "Page",
            "link_to": "agent-workspace",
            "icon": "message-square",
        },
        {
            "label": "Purchase Invoice AI",
            "type": "Link",
            "link_type": "Page",
            "link_to": "purchase-invoice-ai",
            "icon": "receipt-text",
        },
        {"label": "Runs", "type": "Link", "link_type": "DocType", "link_to": "AI Run", "icon": "activity"},
        {
            "label": "Approvals",
            "type": "Link",
            "link_type": "DocType",
            "link_to": "AI Approval Request",
            "icon": "check-circle",
        },
        {"label": "Setup", "type": "Section Break", "icon": "settings", "collapsible": 1},
        {"label": "Agents", "type": "Link", "link_type": "DocType", "link_to": "AI Agent", "icon": "bot"},
        {
            "label": "Gemini OCR Settings",
            "type": "Link",
            "link_type": "DocType",
            "link_to": "Gemini OCR Settings",
            "icon": "scan-text",
        },
        {
            "label": "WhatsApp Support Settings",
            "type": "Link",
            "link_type": "DocType",
            "link_to": "AI WhatsApp Support Settings",
            "icon": "message-circle",
        },
        {"label": "Skills", "type": "Link", "link_type": "DocType", "link_to": "AI Skill", "icon": "zap"},
        {
            "label": "Skill Grants",
            "type": "Link",
            "link_type": "DocType",
            "link_to": "AI Skill Grant",
            "icon": "key-round",
        },
        {"label": "AI Providers", "type": "Link", "link_type": "DocType", "link_to": "AI Provider", "icon": "settings"},
        {"label": "Settings", "type": "Link", "link_type": "DocType", "link_to": "AI Settings", "icon": "settings"},
    ]


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


def ensure_default_provider():
    """Seed one AI Provider from the legacy AI Settings connection so existing
    sites keep working unchanged; later providers are managed in the UI."""
    if not frappe.db.exists("DocType", "AI Provider"):
        return
    if frappe.db.count("AI Provider"):
        return
    settings = frappe.get_doc("AI Settings", "AI Settings")
    provider = frappe.get_doc({
        "doctype": "AI Provider",
        "provider_name": "Default",
        "enabled": 1,
        "is_default": 1,
        "provider_type": settings.provider_type or "OpenAI",
        "base_url": settings.base_url or "",
        "default_model": settings.default_model or "",
        "description": "Seeded from AI Settings on first migrate. Add more providers (DeepSeek, Groq, Ollama, ...) as separate records.",
        "models": (
            [{"model_name": settings.default_model, "is_default": 1}]
            if settings.default_model else []
        ),
    })
    try:
        provider.api_key = settings.get_password("api_key") or ""
    except Exception:
        provider.api_key = ""
    provider.insert(ignore_permissions=True)


def create_default_gemini_ocr_settings():
    """Create Gemini OCR Settings without storing secrets."""
    if not frappe.db.exists("DocType", "Gemini OCR Settings"):
        return

    defaults = {
        "enabled": 0,
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-3.5-flash",
        "prompt": (
            "Extract all visible text from this invoice or document image. Preserve line breaks, "
            "numbers, currency values, item tables, Arabic text, and English text. Return plain text only."
        ),
        "fallback_to_tesseract": 0,
        "request_timeout": 25,
        "max_inline_image_bytes": 8000000,
        "max_output_chars": 8000,
    }
    if not frappe.db.exists("Gemini OCR Settings", "Gemini OCR Settings"):
        frappe.get_doc({"doctype": "Gemini OCR Settings", **defaults}).insert(ignore_permissions=True)
        return

    settings = frappe.get_doc("Gemini OCR Settings", "Gemini OCR Settings")
    changed = False
    for field, value in defaults.items():
        if settings.get(field) in (None, ""):
            settings.set(field, value)
            changed = True
    if cint(settings.get("fallback_to_tesseract")) and settings.get("model") == defaults["model"]:
        settings.fallback_to_tesseract = 0
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


def ensure_whatsapp_support_configuration():
    """Create WhatsApp support agent/settings if their doctypes are available."""
    try:
        from ai_agent.whatsapp_support.setup import ensure_whatsapp_support_configuration as ensure

        ensure()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "AI WhatsApp Support Setup Failed")


def ensure_chatbot_configuration():
    """Create System Chatbot agent/settings if their doctypes are available."""
    try:
        from ai_agent.chatbot.setup import ensure_chatbot_configuration as ensure

        ensure()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "AI Chatbot Setup Failed")


def sync_skills():
    """Sync skills from app files."""
    from ai_agent.runtime.skill_registry import SkillRegistry
    registry = SkillRegistry()
    registry.sync_app_skills()


def ensure_purchase_invoice_agent_configuration():
    """Keep the OCR Journal Entry agent settings stable across migrations."""
    from ai_agent.purchase_invoice_ai.api import _ensure_purchase_invoice_agent

    _ensure_purchase_invoice_agent()


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
            "compact_session": (
                "Session purpose: read-only ERP analysis.\n"
                "Session rules: answer from discovered ERP data, keep filters/company/date explicit, do not perform writes, and do not reuse failed prior-run output as factual data."
            ),
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
            "compact_session": (
                "Session purpose: ERP document preparation and approved operations.\n"
                "Session rules: build one clean payload per target document, use approval tools for writes, group missing prerequisite records, and keep prior chat separate from current tool evidence."
            ),
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
            "compact_session": (
                "Session purpose: high-capacity ERP operations for large or compound workflows.\n"
                "Session rules: use compact summaries, avoid repeating failed prior attempts, preserve the current objective across approvals, and pass named context to tools/results without mixing it with parent_run history."
            ),
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
    skills = [
        skill
        for skill in skills
        if skill == "core-agent-behavior"
        or not frappe.db.get_value("AI Skill", skill, "allow_implicit_invocation")
    ]
    if agent == "ERP Analyst":
        excluded = {"document-creation", "document-workflow"}
        return [skill for skill in skills if skill not in excluded]
    return list(skills)
