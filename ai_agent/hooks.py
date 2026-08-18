app_name = "ai_agent"
app_title = "AI Agent"
app_publisher = "MYSYS"
app_description = "AI Agent for Frappe/ERPNext - Data Analysis and Document Operations"
app_email = "admin@mysys.com"
app_license = "mit"

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

# Override whitelisted methods if needed
# override_whitelisted_methods = {}

