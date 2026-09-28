app_name = "ai_agent"
app_title = "AI Agent"
app_publisher = "MYSYS"
app_description = "AI Agent for Frappe/ERPNext - Data Analysis and Document Operations"
app_email = "admin@mysys.com"
app_license = "mit"

# MCP OAuth discovery must advertise the exact protected endpoint while the
# rest of the Frappe API keeps its normal origin-level resource metadata.
after_request = ["ai_agent.mcp.http.after_request"]

# Includes in <head>
# app_include_css = "/assets/ai_agent/css/agent_workspace.css"
# app_include_js = "/assets/ai_agent/js/agent_workspace.js"

# Floating AI chatbot / technical support widget
app_include_css = "/assets/ai_agent/css/chatbot_widget.css"
app_include_js = "/assets/ai_agent/js/chatbot_widget.js"

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
    ],
    "daily": [
        "ai_agent.runtime.memory_service.clear_expired",
    ],
}

# WhatsApp Support Engine
doc_events = {
    "WhatsApp Webhook Message Received Log": {
        "after_insert": "ai_agent.whatsapp_support.events.on_message_received",
    },
}

# Conversations are private to their owner; see ai_agent/permissions.py.
permission_query_conditions = {
    "AI Run": "ai_agent.permissions.ai_run_query",
    "AI Chat Session": "ai_agent.permissions.ai_chat_session_query",
    "AI Chatbot Conversation": "ai_agent.permissions.ai_chatbot_conversation_query",
    "AI Memory": "ai_agent.permissions.ai_memory_query",
    "AI Tool Call": "ai_agent.permissions.ai_tool_call_query",
    "AI Approval Request": "ai_agent.permissions.ai_approval_request_query",
    "Financial Attachment Session": "ai_agent.permissions.financial_attachment_session_query",
}

has_permission = {
    "AI Run": "ai_agent.permissions.has_owned_permission",
    "AI Chat Session": "ai_agent.permissions.has_owned_permission",
    "AI Chatbot Conversation": "ai_agent.permissions.has_owned_permission",
    "AI Memory": "ai_agent.permissions.has_owned_permission",
    "AI Tool Call": "ai_agent.permissions.has_owned_permission",
    "AI Approval Request": "ai_agent.permissions.has_approval_permission",
    "Financial Attachment Session": "ai_agent.permissions.has_owned_permission",
}

# Override whitelisted methods if needed
# override_whitelisted_methods = {}
