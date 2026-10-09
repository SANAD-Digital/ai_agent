from ai_agent.query.schema import QUERY_SPEC_SCHEMA


OBJECT_SCHEMA = {"type": "object", "properties": {}}
DOCUMENT_PAYLOAD_SCHEMA = {
    "type": "object",
    "description": "Frappe document field values. Do not include ignore_permissions, user, run_as, or commit.",
}

EXPORT_SOURCE = {
    "title": {"type": "string", "description": "Short title in the user's language; also used for the file name"},
    "artifact": {"type": "string", "description": "Preferred source: artifact_name of a table result in this conversation (data.query and reports.run return artifact.artifact_name). The rows are not sent again."},
    "query": {"type": "object", "description": "Otherwise: the data.query arguments (doctype, fields, filters, group_by, order_by, limit) to fetch the rows here with the user's permissions"},
    "columns": {"type": "array", "items": {}, "description": "Optional: column keys in order, or {key, label}"},
    "rows": {"type": "array", "items": {"type": "object"}, "description": "Only for a few inline rows (e.g. KPI lines) that came from no query"},
    "doctype": {"type": "string", "description": "Optional: DocType of the rows, for column labels"},
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
        "properties": {
            "doctype": {"type": "string"},
            "fields_filter": {
                "type": "array", "items": {"type": "string"},
                "description": "Only fields whose name or label contains one of these terms",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 200, "description": "Max fields (default 40, most-used first)"},
        },
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
    "metrics.query": {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "description": "Metric key from the metric catalog"},
            "period": {
                "description": "today, yesterday, this_week, last_week, this_month, last_month, this_quarter, "
                "last_quarter, this_fiscal_year, last_fiscal_year, previous_fiscal_year, or {from, to}",
                "anyOf": [
                    {"type": "string"},
                    {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}}},
                ],
            },
            "dimensions": {"type": "array", "items": {"type": "string"}, "description": "Group by these (metric's dims, or company)"},
            "filters": {"type": "object", "description": "{dimension: value | [values] | '%like%'}"},
            "compare": {"type": "string", "enum": ["previous_period", "same_period_last_year", "yesterday_same_time"]},
            "top": {"type": "integer", "minimum": 1, "maximum": 50},
            "sort": {"type": "string", "enum": ["value_desc", "value_asc", "dimension_asc", "dimension_desc"]},
            "company": {"type": "string"},
        },
        "required": ["metric"],
    },
    "tools.request_more": {
        "type": "object",
        "properties": {
            "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool names to add, e.g. data.query"},
            "reason": {"type": "string"},
        },
        "required": ["tools"],
    },
    "diagnose.document": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "name": {"type": "string"},
            "company": {"type": "string", "description": "For Item: the company to check Item Defaults for"},
        },
        "required": ["doctype", "name"],
    },
    "knowledge.search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What the user wants to know, in their words"},
            "top": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    },
    "setup.check": {
        "type": "object",
        "properties": {
            "area": {"type": "string", "enum": ["company", "accounts", "stock", "selling", "buying"]},
            "company": {"type": "string"},
        },
        "required": ["area"],
    },
    "erp.item_details": {
        "type": "object",
        "properties": {
            "item_code": {"type": "string"}, "company": {"type": "string"},
            "doctype": {"type": "string", "description": "Transaction type, e.g. Sales Invoice or Purchase Order"},
            "customer": {"type": "string"}, "supplier": {"type": "string"}, "price_list": {"type": "string"},
            "currency": {"type": "string"}, "warehouse": {"type": "string"}, "qty": {"type": "number"}, "date": {"type": "string"},
        },
        "required": ["item_code"],
    },
    "erp.party_details": {
        "type": "object",
        "properties": {
            "party_type": {"type": "string", "enum": ["Customer", "Supplier"]}, "party": {"type": "string"},
            "company": {"type": "string"}, "doctype": {"type": "string"}, "date": {"type": "string"},
        },
        "required": ["party_type", "party"],
    },
    "erp.fiscal_year": {
        "type": "object",
        "properties": {"date": {"type": "string"}, "company": {"type": "string"}},
    },
    "erp.exchange_rate": {
        "type": "object",
        "properties": {"from_currency": {"type": "string"}, "to_currency": {"type": "string"}, "date": {"type": "string"}},
        "required": ["from_currency", "to_currency"],
    },
    "erp.stock_balance": {
        "type": "object",
        "properties": {"item_code": {"type": "string"}, "warehouse": {"type": "string"}, "date": {"type": "string"}},
        "required": ["item_code", "warehouse"],
    },
    "erp.invoice_payments": {
        "type": "object",
        "properties": {"doctype": {"type": "string", "enum": ["Sales Invoice", "Purchase Invoice"]}, "name": {"type": "string"}},
        "required": ["doctype", "name"],
    },
    "actions.request_mapped_draft": {
        "type": "object",
        "properties": {
            "source_doctype": {"type": "string", "enum": ["Sales Order", "Material Request", "Purchase Invoice"]},
            "source_name": {"type": "string"},
            "target_doctype": {"type": "string", "enum": ["Sales Invoice", "Delivery Note", "Purchase Order", "Purchase Receipt"]},
        },
        "required": ["source_doctype", "source_name", "target_doctype"],
    },
    "analysis.sales": {
        "type": "object",
        "properties": {
            "period": {"type": "string", "enum": ["today", "yesterday", "this_week", "last_week", "this_month", "last_month", "this_quarter", "last_quarter", "this_fiscal_year"],
                       "description": "Named period, default this_month"},
            "company": {"type": "string", "description": "Company to analyse; required when companies use different currencies"},
            "top": {"type": "integer", "minimum": 1, "maximum": 20, "description": "How many top customers to rank, default 5"},
        },
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
    "files.read": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "File id or file name from the list; omit to list this conversation's attachments"},
            "refresh": {"type": "boolean", "description": "Read again instead of using the cached extraction"},
        },
    },
    "attachments.list": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string"},
            "document_name": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["doctype", "document_name"],
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
            "memory_type": {"type": "string", "description": "Context, Summary, or Mapping. Defaults to Context."},
            "company": {"type": "string"},
            "agent": {"type": "string"},
            "sensitivity": {"type": "string", "description": "Low, Medium, or High. Defaults to Medium."},
            "entity_type": {"type": "string", "description": "For Mapping memories: Supplier, Cash Supplier, Customer, Account, Item, ..."},
            "entity_key": {"type": "string", "description": "For Mapping memories: the entity name (normalized automatically)."},
            "mapping": {"type": "object", "description": "For Mapping memories: the structured fact, e.g. {\"account\": \"...\"}."},
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
    "export.excel": {"type": "object", "properties": EXPORT_SOURCE},
    "export.csv": {"type": "object", "properties": EXPORT_SOURCE},
    "export.pdf": {
        "type": "object",
        "properties": {
            **EXPORT_SOURCE,
            "summary": {"type": "string", "description": "Optional short findings printed above the table (plain text, one point per line)"},
        },
    },
    "export.image": {
        "type": "object",
        "properties": {
            **EXPORT_SOURCE,
            "chart": {"type": "object", "description": "Draw this chart as a PNG instead of a table: {type: bar|line|pie|donut, labels: [...], datasets: [{name, values}]}"},
        },
    },
    "export.print": {
        "type": "object",
        "properties": {
            "doctype": {"type": "string", "description": "DocType of the document, e.g. Sales Invoice"},
            "name": {"type": "string", "description": "Document name (ID), e.g. ACC-SINV-2026-00041"},
            "print_format": {"type": "string", "description": "Optional print format; the DocType's default otherwise"},
            "letterhead": {"type": "boolean", "description": "Print with the company letterhead (default true)"},
        },
        "required": ["doctype", "name"],
    },
    "export.statement": {
        "type": "object",
        "properties": {
            "party_type": {"type": "string", "enum": ["Customer", "Supplier", "Employee", "Shareholder"]},
            "party": {"type": "string", "description": "The party as the user wrote it: ID or name. The tool resolves it and returns candidates when several match"},
            "from_date": {"type": "string", "description": "YYYY-MM-DD; default 12 months before to_date"},
            "to_date": {"type": "string", "description": "YYYY-MM-DD; default today"},
            "company": {"type": "string", "description": "Default: the user's default company"},
            "language": {"type": "string", "enum": ["ar", "en"], "description": "The language the user is writing in"},
        },
        "required": ["party_type", "party"],
    },
    "messaging.request_email": {
        "type": "object",
        "properties": {
            "recipients": {"type": "array", "items": {"type": "string"}, "description": "Email addresses (look them up on the party's Contact if the user named a party)"},
            "cc": {"type": "array", "items": {"type": "string"}},
            "subject": {"type": "string"},
            "message": {"type": "string", "description": "Plain-text body in the user's language; short and polite"},
            "attachments": {"type": "array", "items": {"type": "string"}, "description": "file_url values of files made in this conversation (export.* results)"},
            "reference_doctype": {"type": "string", "description": "Optional document the email is about (logged on its timeline)"},
            "reference_name": {"type": "string"},
        },
        "required": ["recipients", "subject", "message"],
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
            {"name": "tools.request_more", "description": "Ask for extra tools when the ones offered cannot answer the request", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.runtime.router.handle_request_more_stub"},
            {"name": "metrics.query", "description": "Answer a business number (sales, balances, receivables...) from the metric catalog, grouped and compared, permission-scoped", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.metrics.handle_query"},
            {"name": "diagnose.document", "description": "Read-only checks explaining why a document is missing from stock ledger, general ledger, party statement, or why an item does not show in search", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.diagnose.handle_document"},
            {"name": "knowledge.search", "description": "Search the user guides for how-to answers; returns short passages with source and page to cite", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.knowledge.handle_search"},
            {"name": "setup.check", "description": "Read the ERPNext setup of one area and list what is missing, with steps (read only)", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.setup.handle_check"},
            {"name": "erp.item_details", "description": "ERPNext item details for a transaction: price, uom, stock, default accounts", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.erpnext_native.handle_item_details"},
            {"name": "erp.party_details", "description": "ERPNext party details: account, currency, price list, payment terms, taxes", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.erpnext_native.handle_party_details"},
            {"name": "erp.fiscal_year", "description": "Fiscal year for a date and company", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.erpnext_native.handle_fiscal_year"},
            {"name": "erp.exchange_rate", "description": "ERPNext exchange rate between two currencies on a date", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.erpnext_native.handle_exchange_rate"},
            {"name": "erp.stock_balance", "description": "Stock quantity and value of an item in a warehouse on a date", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.erpnext_native.handle_stock_balance"},
            {"name": "erp.invoice_payments", "description": "Payments allocated against a sales or purchase invoice", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.erpnext_native.handle_invoice_payments"},
            {"name": "analysis.sales", "description": "Sales analysis for a period: total sales, invoice and customer counts, average invoice and the top customers with their share (read only, permission-scoped)", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.analysis.handle_sales"},
            {"name": "data.query", "description": "Execute a query specification", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_query"},
            {"name": "data.get_document", "description": "Get a document by name", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_get_document"},
            {"name": "data.get_child_records", "description": "Get child table records", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_get_child_records"},
            {"name": "data.count", "description": "Count records matching filters", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.data.handle_count"},
            {"name": "files.read", "description": "List the files the user attached in this chat conversation, or read one (text of PDF/Word/Excel/CSV, OCR of images and scans). Use it whenever the user refers to a file or image sent earlier.", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.chat_files.handle_read", "model_result_max_chars": 40000},
            {"name": "attachments.list", "description": "List safe metadata for files attached to a readable document", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.attachments.handle_list"},
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
            # Plugins (gated per user by AI Settings access rules)
            {"name": "export.excel", "description": "Give the user an Excel (.xlsx) file of a table result", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.exports.handle_excel"},
            {"name": "export.csv", "description": "Give the user a CSV file of a table result", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.exports.handle_csv"},
            {"name": "export.pdf", "description": "Give the user a PDF report: title, optional summary lines and the table", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.exports.handle_pdf"},
            {"name": "export.print", "description": "Give the user a document as a PDF in its print format (invoice, order, payment...)", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.exports.handle_print"},
            {"name": "export.statement", "description": "Give the user a statement of account PDF for a customer/supplier: opening balance, entries, closing balance", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.exports.handle_statement"},
            {"name": "messaging.request_email", "description": "Request to send an email (optionally with files from this conversation); it is sent only after the user approves", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.messaging.build_payload", "requires_approval": True},
            {"name": "export.image", "description": "Give the user a PNG image of a chart or of a short table", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.exports.handle_image"},
            {"name": "artifacts.create_document_links", "description": "Create document links artifact", "access_level": "Read", "side_effect": "read_only", "handler": "ai_agent.tools.artifacts.handle_create_document_links"},
            # Actions
            {"name": "actions.prepare_document", "description": "Prepare a document draft without saving", "access_level": "Prepare", "side_effect": "prepare_only", "handler": "ai_agent.tools.actions.handle_prepare"},
            {"name": "actions.validate_document", "description": "Validate a document payload", "access_level": "Prepare", "side_effect": "prepare_only", "handler": "ai_agent.tools.actions.handle_validate"},
            {"name": "actions.request_create_draft", "description": "Request approval to create a draft document", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_create", "requires_approval": True},
            {"name": "actions.request_bulk_create", "description": "Request one approval to create multiple draft documents in one batch", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.actions.handle_request_bulk_create", "requires_approval": True},
            {"name": "actions.request_mapped_draft", "description": "Request approval to create a draft from an existing document with ERPNext's standard mapping (Sales Order → Sales Invoice / Delivery Note, Material Request → Purchase Order, Purchase Invoice → Purchase Receipt)", "access_level": "Write", "side_effect": "write", "handler": "ai_agent.tools.erpnext_native.handle_request_mapped_draft", "requires_approval": True},
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

