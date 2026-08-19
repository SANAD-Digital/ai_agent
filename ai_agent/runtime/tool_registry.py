from ai_agent.query.schema import QUERY_SPEC_SCHEMA


OBJECT_SCHEMA = {"type": "object", "properties": {}}
DOCUMENT_PAYLOAD_SCHEMA = {
    "type": "object",
    "description": "Frappe document field values. Do not include ignore_permissions, user, run_as, or commit.",
}

TOOL_PARAMETER_SCHEMAS = {
    "skills.list": {
        "type": "object",
        "properties": {"agent": {"type": "string"}},
    },
    "skills.load": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
            "agent": {"type": "string"},
        },
        "required": ["skill_name"],
    },
    "skills.read_reference": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
            "reference": {"type": "string"},
        },
        "required": ["skill_name"],
    },
    "schema.search_doctypes": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "DocType name, module, or description to search for"},
            "module": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
    },
    "schema.describe_doctype": {
        "type": "object",
        "properties": {"doctype": {"type": "string"}},
        "required": ["doctype"],
    },
    "schema.describe_links": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "depth": {"type": "integer", "minimum": 1, "maximum": 2},
        },
        "required": ["doctype"],
    },
    "schema.describe_workflow": {
        "type": "object",
        "properties": {"doctype": {"type": "string"}},
        "required": ["doctype"],
    },
    "data.query": QUERY_SPEC_SCHEMA,
    "data.get_document": {
        "type": "object",
        "properties": {"doctype": {"type": "string"}, "name": {"type": "string"}},
        "required": ["doctype", "name"],
    },
    "data.get_child_records": {
        "type": "object",
        "properties": {
            "parent_doctype": {"type": "string"},
            "parent_name": {"type": "string"},
            "child_field": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        "required": ["parent_doctype", "parent_name", "child_field"],
    },
    "data.count": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "filters": QUERY_SPEC_SCHEMA["properties"]["filters"],
        },
        "required": ["doctype"],
    },
    "financial_attachments.get_session_ocr_text": {
        "type": "object",
        "properties": {"session_name": {"type": "string"}},
        "required": ["session_name"],
    },
    "reports.search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
    },
    "reports.describe": {
        "type": "object",
        "properties": {"report_name": {"type": "string"}},
        "required": ["report_name"],
    },
    "reports.run": {
        "type": "object",
        "properties": {
            "report_name": {"type": "string"},
            "filters": {"type": "object"},
            "prepared_report_name": {
                "type": "string",
                "description": "Optional Prepared Report document name to read a specific generated report.",
            },
            "generate_if_missing": {
                "type": "boolean",
                "description": "For Prepared Reports, queue generation when no completed result exists. Defaults to true.",
            },
            "sample_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Number of compact sample rows to return. Keep low unless raw review is needed.",
            },
            "include_raw_rows": {
                "type": "boolean",
                "description": "Return raw rows instead of compact rows only when the user explicitly needs row-level detail.",
            },
        },
        "required": ["report_name"],
    },
    "memory.search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keyword, supplier, item, account, or mapping to search in AI memory."},
            "company": {"type": "string"},
            "agent": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    },
    "memory.store_context": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "Compact reusable lesson or mapping confirmed by the user or by a successful approved document action.",
            },
            "scope": {"type": "string", "description": "User, Agent, or Company. Defaults to Agent."},
            "memory_type": {"type": "string", "description": "Context or Summary. Defaults to Context."},
            "company": {"type": "string"},
            "agent": {"type": "string"},
            "sensitivity": {"type": "string", "description": "Low, Medium, or High. Defaults to Medium."},
        },
        "required": ["content"],
    },
    "artifacts.create_table": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short Arabic title shown above the table"},
            "columns": {"type": "array", "items": {"type": "string"}},
            "rows": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["rows"],
    },
    "artifacts.create_chart": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short Arabic title shown above the chart"},
            "chart_spec": {
                "type": "object",
                "description": "Frappe Charts spec: {type: bar|line|pie|percentage, labels: [...], datasets: [{name, values}]}",
            },
        },
        "required": ["chart_spec"],
    },
    "artifacts.create_document_links": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "doctype": {"type": "string"},
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                    },
                },
            },
        },
        "required": ["links"],
    },
    "actions.prepare_document": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "payload": DOCUMENT_PAYLOAD_SCHEMA,
        },
        "required": ["doctype", "payload"],
    },
    "actions.validate_document": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "payload": DOCUMENT_PAYLOAD_SCHEMA,
        },
        "required": ["doctype", "payload"],
    },
    "actions.request_create_draft": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "payload": DOCUMENT_PAYLOAD_SCHEMA,
        },
        "required": ["doctype", "payload"],
    },
    "actions.request_bulk_create": {
        "type": "object",
        "properties": {
            "documents": {
                "type": "array",
                "description": "Multiple draft documents to create after one approval. Use for bulk missing master data such as several Items, Suppliers, Customers, or other prerequisite records.",
                "items": {
                    "type": "object",
                    "properties": {
                        "doctype": {"type": "string"},
                        "payload": DOCUMENT_PAYLOAD_SCHEMA,
                    },
                    "required": ["doctype", "payload"],
                },
            },
            "summary": {
                "type": "string",
                "description": "Short user-facing summary of the batch.",
            },
        },
        "required": ["documents"],
    },
    "actions.request_update": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "document_name": {"type": "string"},
            "payload": DOCUMENT_PAYLOAD_SCHEMA,
            "expected_modified": {"type": "string"},
        },
        "required": ["doctype", "document_name", "payload"],
    },
    "actions.request_submit": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "document_name": {"type": "string"},
        },
        "required": ["doctype", "document_name"],
    },
    "actions.request_cancel": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "document_name": {"type": "string"},
        },
        "required": ["doctype", "document_name"],
    },
    "actions.request_workflow_action": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "document_name": {"type": "string"},
            "action": {"type": "string"},
        },
        "required": ["doctype", "document_name", "action"],
    },
}


class ToolRegistry:
    """Registry of all available tools."""

    def __init__(self):
        self._tools = {}
        self._register_builtins()

    def _register_builtins(self):
        """Register all built-in tools."""
        tools = [
            # Skills
            {"name": "skills.list", "description": "List available skills", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.skills.handle_list"},
            {"name": "skills.load", "description": "Load a skill's instructions and tools", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.skills.handle_load", "model_result_max_chars": 32768},
            {"name": "skills.read_reference", "description": "Read skill reference documentation", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.skills.handle_read_reference", "model_result_max_chars": 32768},
            # Schema
            {"name": "schema.search_doctypes", "description": "Search DocTypes by name/description", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.schema.handle_search_doctypes"},
            {"name": "schema.describe_doctype", "description": "Describe a DocType's fields and relationships", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.schema.handle_describe_doctype"},
            {"name": "schema.describe_links", "description": "Describe link relationships for a DocType", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.schema.handle_describe_links"},
            {"name": "schema.describe_workflow", "description": "Describe workflow for a DocType", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.schema.handle_describe_workflow"},
            # Data
            {"name": "data.query", "description": "Execute a query specification", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_query"},
            {"name": "data.get_document", "description": "Get a document by name", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_get_document"},
            {"name": "data.get_child_records", "description": "Get child table records", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_get_child_records"},
            {"name": "data.count", "description": "Count records matching filters", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_count"},
            {"name": "financial_attachments.get_session_ocr_text", "description": "Get only the ready OCR text from a Financial Attachment Session", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.financial_attachments.handle_get_session_ocr_text", "result_max_bytes": 1048576, "model_result_max_chars": 65536},
            # Reports
            {"name": "reports.search", "description": "Search available reports", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.reports.handle_search"},
            {"name": "reports.describe", "description": "Describe a report's filters and columns", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.reports.handle_describe"},
            {"name": "reports.run", "description": "Run a report with filters", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.reports.handle_run"},
            # Memory
            {"name": "memory.search", "description": "Search reusable AI memory for mappings and prior confirmed context", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.memory.handle_search"},
            {"name": "memory.store_context", "description": "Store reusable AI memory such as confirmed account mappings", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.memory.handle_store_context"},
            # Artifacts
            {"name": "artifacts.create_table", "description": "Create a table artifact", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.artifacts.handle_create_table"},
            {"name": "artifacts.create_chart", "description": "Create a chart artifact", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.artifacts.handle_create_chart"},
            {"name": "artifacts.create_document_links", "description": "Create document links artifact", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.artifacts.handle_create_document_links"},
            # Actions
            {"name": "actions.prepare_document", "description": "Prepare a document draft without saving", "access_level": "Prepare", "side_effect": "prepare_only", "handler": "ai_agent.tools.actions.handle_prepare"},
            {"name": "actions.validate_document", "description": "Validate a document payload", "access_level": "Prepare", "side_effect": "prepare_only", "handler": "ai_agent.tools.actions.handle_validate"},
            {"name": "actions.request_create_draft", "description": "Request approval to create a draft document", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_create", "requires_approval": True},
            {"name": "actions.request_bulk_create", "description": "Request one approval to create multiple draft documents in one batch", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_bulk_create", "requires_approval": True},
            {"name": "actions.request_update", "description": "Request approval to update a document", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_update", "requires_approval": True},
            {"name": "actions.request_submit", "description": "Request approval to submit a document", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_submit", "requires_approval": True},
            {"name": "actions.request_cancel", "description": "Request approval to cancel a document", "access_level": "Destructive", "side_effect": "destructive", "handler": "ai_agent.tools.actions.handle_request_cancel", "requires_approval": True},
            {"name": "actions.request_workflow_action", "description": "Request approval for a workflow action", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_workflow", "requires_approval": True},
        ]
        for tool in tools:
            tool["parameters"] = TOOL_PARAMETER_SCHEMAS.get(tool["name"], OBJECT_SCHEMA)
            self._tools[tool["name"]] = tool

    def get_tool(self, name):
        return self._tools.get(name)

    def list_tools(self):
        return list(self._tools.values())

    def is_registered(self, name):
        return name in self._tools
