app_name = "ai_agent"
app_title = "Musaed"
app_publisher = "MYSYS"
app_description = "AI Agent for Frappe/ERPNext - Data Analysis and Document Operations"
app_email = "admin@mysys.com"
app_license = "mit"

# MCP OAuth discovery must advertise the exact protected endpoint while the
# rest of the Frappe API keeps its normal origin-level resource metadata.
after_request = ["ai_agent.mcp.http.after_request"]

# A token issued to an AI connector (dynamic registration, or a Claude/ChatGPT callback) works on the MCP
# endpoint only, never on /api/resource or other methods (ai_agent/mcp/token_scope.py).
auth_hooks = ["ai_agent.mcp.token_scope.restrict_mcp_tokens"]

# The support bubble on the chat engine (ai_agent/chat/support.py). Only this
# tiny loader ships on every Desk page; the chat itself loads on first open.
# The CSS bundle also carries the Musaed brand kit (public/musaed-brand):
# fonts, --ms-* tokens and the robot's motion states. Bundles, not plain
# files, so a build always reaches browsers despite the year-long asset cache.
app_include_css = "ai_agent.bundle.css"
app_include_js = "ai_agent.bundle.js"

# Installation
after_install = "ai_agent.install.after_install"
after_migrate = "ai_agent.install.after_migrate"

# Scheduled Tasks
scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "ai_agent.runtime.run_control.recover_orphaned_runs",
        ],
    },
    "hourly": [
        "ai_agent.runtime.approval_engine.expire_pending_approvals",
        "ai_agent.engine.memory.extract_idle_sessions",
        "ai_agent.runtime.health.check_and_alert",
    ],
    "daily": [
        "ai_agent.runtime.memory_service.clear_expired",
    ],
}

doc_events = {
    "OAuth Client": {
        "on_update": "ai_agent.mcp.token_scope.clear_cache",
        "on_trash": "ai_agent.mcp.token_scope.clear_cache",
    },
}

# Retention through Log Settings (days; editable there). AI Run clears only runs outside a conversation
# (MCP / API: one run per tool call); chat messages are never cleared automatically.
default_log_clearing_doctypes = {
    "AI Tool Call": 90,
    "AI Run": 180,
}

# Personal Data Deletion Request: a user's conversations (their messages and blocks go with the session's
# on_trash), memory and runs are deleted with the user's data.
user_data_fields = [
    {"doctype": "AI Chat Session", "filter_by": "user"},
    {"doctype": "AI Memory", "filter_by": "user"},
    {"doctype": "AI Run", "filter_by": "user"},
]

# Conversations are private to their owner; see ai_agent/permissions.py.
permission_query_conditions = {
    "AI Run": "ai_agent.permissions.ai_run_query",
    "AI Chat Session": "ai_agent.permissions.ai_chat_session_query",
    "AI Chat Message": "ai_agent.permissions.ai_chat_message_query",
    "AI Message Block": "ai_agent.permissions.ai_message_block_query",
    "AI Memory": "ai_agent.permissions.ai_memory_query",
    "AI Tool Call": "ai_agent.permissions.ai_tool_call_query",
    "AI Approval Request": "ai_agent.permissions.ai_approval_request_query",
}

has_permission = {
    "AI Run": "ai_agent.permissions.has_owned_permission",
    "AI Chat Session": "ai_agent.permissions.has_owned_permission",
    "AI Chat Message": "ai_agent.permissions.has_owned_permission",
    "AI Message Block": "ai_agent.permissions.has_owned_permission",
    "AI Memory": "ai_agent.permissions.has_owned_permission",
    "AI Tool Call": "ai_agent.permissions.has_owned_permission",
    "AI Approval Request": "ai_agent.permissions.has_approval_permission",
}

# Override whitelisted methods if needed
# override_whitelisted_methods = {}
