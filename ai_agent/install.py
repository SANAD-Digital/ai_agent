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
    ensure_mcp_role()
    from ai_agent.roles import ensure_roles

    ensure_roles()
    # Before anything that syncs skills (agents, chatbot): a skill row
    # naming a tool missing from AI Skill Tool's options is rejected.
    sync_tool_name_options()
    create_default_settings()
    create_default_gemini_ocr_settings()
    ensure_default_provider()
    ensure_preset_providers()
    link_default_provider()
    create_default_agents()
    link_model_providers()
    tidy_memory_rows()
    ensure_chatbot_configuration()
    ensure_workspace_sidebar()
    ensure_workspace_sidebar_doc()
    ensure_desktop_icon()
    sync_skills()
    ensure_standard_plugins()
    create_default_skill_grants()
    ensure_default_metrics()
    ensure_new_setting_defaults()
    move_mcp_allowed_roles_to_table()
    frappe.db.commit()


# Check fields added to the AI Settings single after it already exists do not
# get their JSON default on existing sites. Seed them once; a saved value
# (including an admin's explicit 0) is never overwritten.
NEW_SETTING_DEFAULTS = {"enable_tool_routing": 1, "mcp_require_approval": 1, "allow_auto_approval": 1}
# Customization (customize.* tools): every switch is off by default (its JSON default 0 is what an unsaved Check
# reads as); only the non-zero limit needs seeding. Seeded only once the field exists on this site.
CUSTOMIZATION_SETTING_DEFAULTS = {"customization_max_requests_per_hour": 20}
# Skills granted by role, never to "All Users": they change the system itself (design §3.6.1).
ROLE_GRANTED_SKILLS = {"customization": "System Manager"}


def ensure_provider_policy_defaults():
    """Existing sites keep working: if a non-Anthropic provider is already
    configured, the first migrate records Own Key mode with consent (the admin
    configured that provider deliberately). New sites start Managed."""
    stored = frappe.db.sql(
        "select 1 from `tabSingles` where doctype='AI Settings' and field='provider_mode'"
    )
    if stored:
        return
    types = set(frappe.get_all("AI Provider", filters={"enabled": 1}, pluck="provider_type"))
    settings = frappe.get_single("AI Settings")
    if not types and settings.get_password("api_key", raise_exception=False):
        types.add(settings.provider_type or "OpenAI Compatible")
    gemini = frappe.db.exists("DocType", "Gemini OCR Settings") and frappe.db.get_single_value("Gemini OCR Settings", "enabled")
    if types - {"Anthropic"} or gemini:
        frappe.db.set_single_value("AI Settings", "provider_mode", "Own Key")
        frappe.db.set_single_value("AI Settings", "allow_third_party_providers", 1)
    else:
        frappe.db.set_single_value("AI Settings", "provider_mode", "Managed")


def move_mcp_allowed_roles_to_table():
    from ai_agent.mcp.setup import move_allowed_roles_to_table

    move_allowed_roles_to_table()


def ensure_new_setting_defaults():
    ensure_provider_policy_defaults()
    for fieldname, value in NEW_SETTING_DEFAULTS.items():
        stored = frappe.db.sql(
            "select 1 from `tabSingles` where doctype='AI Settings' and field=%s", fieldname
        )
        if not stored:
            frappe.db.set_single_value("AI Settings", fieldname, value)
    meta = frappe.get_meta("AI Settings")
    for fieldname, value in CUSTOMIZATION_SETTING_DEFAULTS.items():
        if not meta.has_field(fieldname):
            continue  # the settings field arrives with its migration; seeded on the migrate after it
        stored = frappe.db.sql(
            "select 1 from `tabSingles` where doctype='AI Settings' and field=%s", fieldname
        )
        if not stored:
            frappe.db.set_single_value("AI Settings", fieldname, value)


def ensure_default_metrics():
    from ai_agent.metrics.defaults import ensure_default_metrics as ensure

    ensure()


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

    # Links, cards and labels come from workspace/ai/ai.json (synced on migrate);
    # here only what an old site may still carry is removed.
    _drop_retired_workspace_entries(workspace)
    _refresh_workspace_card_counts(workspace)
    workspace.save(ignore_permissions=True)


# Pages and DocTypes the app no longer ships; existing sites still carry their links.
RETIRED_WORKSPACE_TARGETS = {"purchase-invoice-ai", "AI Chatbot Conversation", "AI Tool Call"}
RETIRED_SHORTCUT_NAMES = {"Purchase Invoice AI"}


def _drop_retired_workspace_entries(workspace):
    for table in ("links", "shortcuts"):
        rows = [row for row in workspace.get(table) if row.get("link_to") not in RETIRED_WORKSPACE_TARGETS]
        for index, row in enumerate(rows, start=1):
            row.idx = index
        workspace.set(table, rows)
    try:
        content = json.loads(workspace.content or "[]")
    except ValueError:
        # Unparseable workspace content: leave the blocks as they are.
        return
    kept = [
        block for block in content
        if not (block.get("type") == "shortcut" and block.get("data", {}).get("shortcut_name") in RETIRED_SHORTCUT_NAMES)
    ]
    if len(kept) != len(content):
        workspace.content = json.dumps(kept, separators=(",", ":"))


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
        "logo_url": "/assets/ai_agent/musaed-brand/assets/svg/icon/icon-primary.svg",
    }

    if icon_name:
        icon = frappe.get_doc("Desktop Icon", icon_name)
        for field, value in values.items():
            icon.set(field, value)
        icon.save(ignore_permissions=True)
        return

    frappe.get_doc({"doctype": "Desktop Icon", **values}).insert(ignore_permissions=True)


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
    wanted = _ai_workspace_sidebar_items()
    def key(row):
        # only link rows link: Frappe fills section rows with a default link_type
        keys = ("label", "type", "link_type", "link_to", "icon") if row.get("type") == "Link" else ("label", "type", "icon")
        return {k: row.get(k) for k in keys if row.get(k) not in (None, "", 0)}

    current = [key(row) for row in sidebar.get("items") or []]
    target = [key(item) for item in wanted]
    unchanged = not sidebar.is_new() and current == target and all(sidebar.get(f) == v for f, v in values.items())
    # saving re-exports the JSON in developer mode: only when something changed, so a
    # migrate does not leave a timestamp-only diff behind
    if not unchanged:
        for field, value in values.items():
            sidebar.set(field, value)
        sidebar.set("items", [])
        for item in wanted:
            sidebar.append("items", item)
        if sidebar.is_new():
            sidebar.insert(ignore_permissions=True)
        else:
            sidebar.save(ignore_permissions=True)

    for old_name in ("AI Agent", "AI Agent Workspace"):
        if frappe.db.exists("Workspace Sidebar", old_name):
            frappe.delete_doc("Workspace Sidebar", old_name, ignore_permissions=True, force=True)


def _ai_workspace_sidebar_items():
    def link(label, link_to, icon, link_type="DocType"):
        return {"label": label, "type": "Link", "link_type": link_type, "link_to": link_to, "icon": icon}

    def section(label, icon):
        return {"label": label, "type": "Section Break", "icon": icon, "collapsible": 1}

    return [
        link("Home", "AI", "home", "Workspace"),
        link("AI Chat", "agent-workspace", "message-square", "Page"),
        link("Approval Requests", "AI Approval Request", "circle-check"),
        section("Capabilities", "zap"),
        link("Agents", "AI Agent", "bot"),
        link("Skills", "AI Skill", "sparkles"),
        link("Plugins", "AI Plugin", "puzzle"),
        link("Skill Grants", "AI Skill Grant", "key-round"),
        section("Monitoring", "activity"),
        link("Runs", "AI Run", "activity"),
        section("Settings", "settings"),
        link("Settings & Permissions", "AI Settings", "settings"),
        link("Model Providers", "AI Provider", "server"),
        link("Quick Support Window", "AI Chatbot Settings", "message-circle"),
        link("Image Reading (OCR)", "Gemini OCR Settings", "scan-text"),
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
            "enable_memory": 1,
            "memory_retention_days": 90,
            "default_approval_ttl_minutes": 1440,
            "enable_mcp": 0,
            "mcp_allowed_roles": [{"role": "AI MCP User"}, {"role": "System Manager"}],
            "mcp_max_requests_per_minute": 60,
            "mcp_enable_action_requests": 0,
        })
        settings.insert(ignore_permissions=True)
        return

    settings = frappe.get_doc("AI Settings", "AI Settings")
    defaults = {
        # mandatory legacy fields: a settings row left by an earlier install may have them empty
        "provider_type": "OpenAI",
        "default_model": "gpt-4o",
        "request_timeout": 120,
        "maximum_run_seconds": 300,
        "maximum_steps": 12,
        "maximum_tool_calls": 25,
        "maximum_rows_per_query": 500,
        "maximum_result_bytes": 102400,
        "enable_memory": 1,
        "memory_retention_days": 90,
        "default_approval_ttl_minutes": 1440,
        "mcp_max_requests_per_minute": 60,
    }
    changed = False
    for field, value in defaults.items():
        if settings.get(field) in (None, ""):
            settings.set(field, value)
            changed = True
    if changed:
        settings.save(ignore_permissions=True)


def ensure_mcp_role():
    """Create a dedicated MCP access gate without granting ERP permissions."""
    if not frappe.db.exists("Role", "AI MCP User"):
        frappe.get_doc(
            {
                "doctype": "Role",
                "role_name": "AI MCP User",
                "desk_access": 1,
                "is_custom": 1,
            }
        ).insert(ignore_permissions=True)


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
        frappe.log_error(frappe.get_traceback(), "AI provider seed: API key unreadable")
        provider.api_key = ""
    provider.insert(ignore_permissions=True)


# Ready-made providers, created disabled and without a key: the admin pastes the
# key, presses "جلب النماذج من المزوّد", picks models and enables. Created once;
# an existing record (edited or renamed by the admin) is never touched.
def _claude_model(name, label, input_price, output_price, default=0):
    return {
        "model_name": name, "label": label, "model_type": "Chat", "is_default": default,
        "supports_vision": 1, "supports_tools": 1, "supports_thinking": 1, "supports_pdf": 1,
        "context_window_tokens": 1000000, "max_output_tokens": 128000,
        "input_price_per_million": input_price, "output_price_per_million": output_price,
    }


PRESET_PROVIDERS = [
    {
        "provider_name": "Anthropic",
        "provider_type": "Anthropic",
        "default_model": "claude-opus-5-5",
        "description": "Anthropic (Claude) عبر واجهته الأصلية مع التخزين المؤقت للتعليمات (prompt caching)، وهو المزوّد الوحيد المسموح في الوضع المُدار. "
        "أنشئ المفتاح من platform.claude.com، الصقه هنا واحفظ، ثم فعّل المزوّد. النموذج الافتراضي Claude Opus 5.5؛ "
        "Sonnet 5.5 أسرع وأرخص، وHaiku 5.5 للمهام البسيطة.",
        "models": [
            _claude_model("claude-opus-5-5", "Claude Opus 5.5", 4.0, 20.0, default=1),
            _claude_model("claude-sonnet-5-5", "Claude Sonnet 5.5", 2.0, 10.0),
            _claude_model("claude-haiku-5-5", "Claude Haiku 5.5", 0.10, 0.50),
        ],
    },
    {
        "provider_name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "description": "OpenRouter: مئات النماذج من مزوّدين مختلفين (Claude وGPT وGemini وDeepSeek وLlama…) بمفتاح واحد. "
        "أنشئ المفتاح من openrouter.ai/keys، الصقه هنا واحفظ، ثم «جلب النماذج من المزوّد» واختر ما تريد، ثم فعّل المزوّد.",
    },
    {
        "provider_name": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "description": "Google Gemini عبر رابطه المتوافق مع OpenAI. للباقة المجانية: أنشئ المفتاح من aistudio.google.com/apikey "
        "في مشروع غير مربوط بالدفع (المشروع المربوط بالدفع يتوقف حين ينفد رصيده). الصق المفتاح واحفظ، ثم «جلب النماذج من المزوّد» واختر ما تريد، ثم فعّل المزوّد.",
    },
    {
        "provider_name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "description": "Groq: نماذج مفتوحة (Llama وQwen وغيرها) بسرعة استجابة عالية جداً. "
        "أنشئ المفتاح من console.groq.com/keys، الصقه هنا واحفظ، ثم «جلب النماذج من المزوّد» واختر ما تريد، ثم فعّل المزوّد.",
    },
]


def tidy_memory_rows():
    """AI Memory kept "company=…; agent=…;" inside the text (the fields hold it) and had no
    title; clean both once."""
    import re

    from ai_agent.ai_agent.doctype.ai_memory.ai_memory import memory_title

    if not frappe.db.has_column("AI Memory", "title"):
        return
    for row in frappe.get_all("AI Memory", filters={"title": ["is", "not set"]}, fields=["name", "content"]):
        content = re.sub(r"^(\s*(agent|company)=[^;]+;\s*)+", "", row.content or "")
        frappe.db.set_value("AI Memory", row.name, {"content": content, "title": memory_title(content)}, update_modified=False)


def link_model_providers():
    """Agents and the support window named a model with no provider; give each the
    provider that offers that model (the default one first). Runs once per record."""
    def provider_of(model):
        if not model:
            return None
        rows = frappe.get_all("AI Provider Model", filters={"model_name": model, "parenttype": "AI Provider"}, pluck="parent")
        default = frappe.db.get_value("AI Provider", {"is_default": 1}, "name")
        return default if default in rows else (rows[0] if rows else None)

    for agent in frappe.get_all("AI Agent", filters={"provider": ["is", "not set"], "model": ["is", "set"]}, fields=["name", "model"]):
        found = provider_of(agent.model)
        if found:
            frappe.db.set_value("AI Agent", agent.name, "provider", found, update_modified=False)
    if frappe.db.exists("DocType", "AI Chatbot Settings") and not frappe.db.get_single_value("AI Chatbot Settings", "provider"):
        found = provider_of(frappe.db.get_single_value("AI Chatbot Settings", "model"))
        if found:
            frappe.db.set_single_value("AI Chatbot Settings", "provider", found)


def link_default_provider():
    """AI Settings > Default Provider (a link) names the provider marked default."""
    if frappe.db.get_single_value("AI Settings", "default_provider"):
        return
    default = frappe.db.get_value("AI Provider", {"is_default": 1, "enabled": 1}, "name")
    if default:
        frappe.db.set_single_value("AI Settings", "default_provider", default)


def ensure_preset_providers():
    if not frappe.db.exists("DocType", "AI Provider"):
        return
    for preset in PRESET_PROVIDERS:
        if frappe.db.exists("AI Provider", preset["provider_name"]):
            continue
        frappe.get_doc({
            "doctype": "AI Provider",
            "provider_type": "OpenAI Compatible",
            "enabled": 0,
            "is_default": 0,
            **preset,
        }).insert(ignore_permissions=True)


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


# Plugins the app ships. Their text is app-managed (refreshed on migrate); an
# admin may disable one, and may add own plugins (is_standard = 0) that stay untouched.
# Title and description are English and shown translated (context "AI Chat").
STANDARD_PLUGINS = [
    {
        "plugin_name": "excel", "title": "Excel", "output": "Excel", "icon": "file-spreadsheet", "tool": "export.excel",
        "description": "Get the result as an Excel file ready to download",
        "instructions": "Get the rows the request needs (data.query, reports.run or metrics.query), then call export.excel once. Pass `artifact` when the result has artifact.artifact_name, otherwise pass the same `query`; send inline `rows` only for a few computed lines. Columns in a sensible order with the user's language labels. Reply with one or two sentences about what the file holds.",
    },
    {
        "plugin_name": "pdf", "title": "PDF Report", "output": "PDF", "icon": "file-text", "tool": "export.pdf",
        "description": "A PDF report with a title, a summary and a table",
        "instructions": "Get the rows the request needs, then call export.pdf once with a clear `title`, a `summary` of 2-5 short findings (one per line, numbers included) and the table via `artifact` or `query`. Keep only the columns a reader needs (at most 8). Reply briefly; the user downloads the file.",
    },
    {
        "plugin_name": "image", "title": "Image", "output": "Image", "icon": "image", "tool": "export.image",
        "description": "A PNG image of a chart or a short table, to share",
        "instructions": "Prefer a chart: compute the aggregated values (at most ~15 labels) and call export.image with `chart` {type, labels, datasets}. For a short list (≤30 rows) pass the table via `artifact`/`query`/`rows` instead. Reply briefly.",
    },
    {
        "plugin_name": "chart", "title": "Chart", "output": "Chart", "icon": "bar-chart-2", "tool": "artifacts.create_chart",
        "description": "An interactive chart inside the conversation",
        "instructions": "Aggregate the data first (group by the dimension the user asked about, at most ~20 labels), then call artifacts.create_chart with an Arabic title. bar for comparisons, line for time series, pie/donut for shares. Add one or two sentences on what the chart shows.",
    },
    {
        "plugin_name": "print", "title": "Print a Document", "output": "PDF", "icon": "printer", "tool": "export.print",
        "description": "The document as a PDF in its official print format: invoice, order, voucher",
        "instructions": "Find the exact document the user means (data.query on its DocType with the customer/date/number they gave; ask only if several match), then call export.print once with doctype and name. Use the default print format unless the user named one. Reply with one line naming the document.",
    },
    {
        "plugin_name": "statement", "title": "Account Statement", "output": "PDF", "icon": "file-text", "tool": "export.statement",
        "description": "A customer or supplier statement as a PDF: opening, transactions and closing balance",
        "instructions": "Call export.statement directly with party_type, party exactly as the user wrote it (ID or name; the tool resolves it), the period the user asked for (default: last 12 months to today) and language = the language the user writes in. If it returns candidates, ask the user which one in one line. Reply with the closing balance in one sentence (له/عليه) and nothing else.",
    },
    {
        "plugin_name": "email", "title": "Send by Email", "output": "Other", "icon": "mail", "tool": "messaging.request_email",
        "description": "Emails the result or the file after you approve",
        "instructions": "Prepare what to send first (e.g. export.statement, export.print or export.excel) unless a file from this conversation already fits. Find the recipient's email: use the address the user gave, otherwise the party's Contact (data.query on Contact Email / the party's email_id); never guess an address. Then call messaging.request_email once with recipients, a short subject, a short polite message in the user's language and the file_url values as attachments. The email goes out only after the user approves the card.",
    },
    {
        "plugin_name": "csv", "title": "CSV", "output": "CSV", "icon": "file", "tool": "export.csv",
        "description": "A CSV file of the raw data",
        "instructions": "Get the rows, then call export.csv once with `artifact` or `query` (inline `rows` only for a few lines). Reply briefly.",
    },
]


def ensure_standard_plugins():
    if not frappe.db.exists("DocType", "AI Plugin"):
        return
    for spec in STANDARD_PLUGINS:
        values = {**spec, "is_standard": 1}
        if frappe.db.exists("AI Plugin", spec["plugin_name"]):
            doc = frappe.get_doc("AI Plugin", spec["plugin_name"])
            changed = False
            for field, value in values.items():
                if doc.get(field) != value:
                    doc.set(field, value)
                    changed = True
            if changed:
                doc.save(ignore_permissions=True)
        else:
            frappe.get_doc({"doctype": "AI Plugin", "enabled": 1, **values}).insert(ignore_permissions=True)


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
        # The cached meta still holds the old options; without this, the skill
        # sync that follows rejects newly registered tools on a fresh migrate.
        frappe.clear_cache(doctype="AI Skill Tool")


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
        if "customization" not in allowed_skills:
            # earlier versions granted it to every acting agent; it moved to the Implementation agent
            for grant in frappe.get_all("AI Skill Grant", filters={"agent": agent_docname, "skill": "customization", "enabled": 1}, pluck="name"):
                frappe.db.set_value("AI Skill Grant", grant, "enabled", 0)
        for skill in allowed_skills:
            if skill in ROLE_GRANTED_SKILLS:
                _ensure_role_skill_grant(agent_docname, skill, ROLE_GRANTED_SKILLS[skill])
                continue
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
            "allow_customization": 0,
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
            "allow_customization": 0,
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
            "allow_customization": 0,
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
        "Implementation": {
            "description": "Frappe implementation agent: fields, properties, scripts, reports, print formats, notifications, workflows, workspaces, cards and charts, list settings, naming, permissions, web pages and forms, custom DocTypes",
            "enabled": 1,
            "is_default": 0,
            "read_only": 0,
            "allow_actions": 1,
            "allow_customization": 1,
            "require_approval_for_all_writes": 1,
            "reasoning_effort": "high",
            "maximum_steps": 60,
            "maximum_tool_calls": 120,
            "compact_session": (
                "Session purpose: Frappe implementation and customization through the customize tools.\n"
                "Session rules: inspect the target first, read the customization skill reference for the kind before writing "
                "code, run customize.check until it is clean, then request; one request per target; never use the generic "
                "document actions for code, fields, reports or any other customization."
            ),
            "system_instructions": (
                "You are the Implementation agent: a senior Frappe/ERPNext implementer. You add custom fields, change field and "
                "DocType properties, write client scripts, server scripts and reports, and build print formats, notifications, "
                "workflows, workspaces, number cards and charts, list settings, naming rules, role permissions, web pages and "
                "forms and custom DocTypes, always through the customize tools. "
                "Before writing any code, load the customization skill and read the reference for that kind; use only APIs the "
                "reference lists for Frappe v16. Inspect the DocType first (real fieldnames only, never guessed), run "
                "customize.check and fix every critical finding before requesting, and explain the change, its risk and how to undo "
                "it in plain words. Every change waits for the user's approval in Desk; never claim a change is done before it is "
                "executed and verified. Prefer configuration (fields, properties, list settings) over code, and client-side over "
                "server-side code when both would work."
            ),
        },
    }


def _get_agent_docname(agent_name):
    return (
        frappe.db.exists("AI Agent", agent_name)
        or frappe.db.exists("AI Agent", {"agent_name": agent_name})
    )


def _ensure_role_skill_grant(agent, skill, role):
    """One enabled grant of ``skill`` to ``role`` on ``agent``; an admin who disabled it keeps it disabled."""
    if not frappe.db.exists("Role", role):
        return
    if frappe.db.exists("AI Skill Grant", {"agent": agent, "skill": skill, "grant_type": "Role", "role": role}):
        return
    frappe.get_doc({
        "doctype": "AI Skill Grant",
        "agent": agent,
        "skill": skill,
        "grant_type": "Role",
        "role": role,
        "enabled": 1,
    }).insert(ignore_permissions=True)


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
        # read-only agent: no document creation or workflow playbook, and no changes to the system itself
        excluded = {"documents", "customization"}
        return [skill for skill in skills if skill not in excluded]
    if not _standard_agents().get(agent, {}).get("allow_customization"):
        # customization belongs to the Implementation agent
        return [skill for skill in skills if skill != "customization"]
    return list(skills)
