"""Bridge MCP tools to AI Agent's existing permission-aware tool gateway."""

import copy
import importlib
import json
import time

import frappe
from frappe.utils import cint

from ai_agent.mcp.protocol import MCPProtocolError, tool_result
from ai_agent.runtime.exceptions import PermissionDenied, ToolDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.tool_gateway import ToolGateway
from ai_agent.runtime.tool_registry import ToolRegistry


ACTION_PREFIX = "actions.request_"
SAFE_SIDE_EFFECTS = {"read_only", "prepare_only"}

# Human-readable titles for MCP clients that show them instead of tool names.
TOOL_TITLES = {
    "actions.request_bulk_create": "طلب إنشاء عدة مسودات (بموافقة)",
    "actions.request_create_draft": "طلب إنشاء مسودة (بموافقة)",
    "actions.request_mapped_draft": "طلب إنشاء مسودة من مستند (بموافقة)",
    "actions.request_update": "طلب تعديل مستند (بموافقة)",
    "actions.request_workflow_action": "طلب إجراء سير عمل (بموافقة)",
    "actions.validate_document": "التحقق من بيانات مستند",
    "artifacts.create_chart": "إنشاء رسم بياني",
    "artifacts.create_document_links": "إنشاء روابط مستندات",
    "artifacts.create_table": "إنشاء جدول",
    "attachments.list": "عرض المرفقات",
    "data.count": "عدّ السجلات",
    "data.get_child_records": "قراءة صفوف جدول فرعي",
    "data.get_document": "قراءة مستند",
    "data.query": "استعلام بيانات",
    "diagnose.document": "تشخيص مستند",
    "erp.exchange_rate": "سعر الصرف",
    "erp.fiscal_year": "السنة المالية",
    "erp.invoice_payments": "مدفوعات الفاتورة",
    "erp.item_details": "تفاصيل صنف",
    "erp.party_details": "تفاصيل عميل/مورد",
    "erp.stock_balance": "رصيد المخزون",
    "financial_attachments.get_session_ocr_text": "نص المرفق المالي (OCR)",
    "knowledge.search": "بحث في قاعدة المعرفة",
    "memory.search": "بحث في الذاكرة",
    "metrics.query": "استعلام مؤشر",
    "reports.describe": "وصف تقرير",
    "reports.run": "تشغيل تقرير",
    "reports.search": "بحث عن تقرير",
    "schema.describe_doctype": "وصف بنية نوع مستند",
    "schema.describe_links": "روابط نوع مستند",
    "schema.describe_workflow": "سير عمل نوع مستند",
    "schema.search_doctypes": "بحث عن أنواع المستندات",
    "setup.check": "فحص الإعداد",
    "skills.list": "عرض المهارات",
    "skills.load": "تحميل مهارة",
    "skills.read_reference": "قراءة مرجع مهارة",
}
# MCP clients cannot render artifacts inline; tell the model what to do instead.
MCP_DESCRIPTION_NOTES = {
    "artifacts.create_chart": (
        "Returns chart_url: a link that opens the drawn chart in the browser."
        " Always give chart_url to the user as a clickable link."
    ),
}


class MCPService:
    def __init__(self, user=None):
        self.user = user or frappe.session.user
        self.settings = frappe.get_cached_doc("AI Settings")
        self._assert_access()
        self.agent = self._get_agent()
        self.company = self.settings.get("mcp_company") or None
        self.registry = ToolRegistry()

    def instructions(self):
        mode = "read-only"
        if self._action_requests_enabled():
            mode = "read access plus approval-gated action requests"
        return (
            "ERPNext/Frappe tools with server-side user permissions. "
            f"This connection provides {mode}. Discover DocTypes and schemas before querying. "
            "Never invent document names or field names. Action-request tools do not modify "
            "documents directly; they create a pending approval in Frappe."
        )

    def list_tools(self):
        tools = []
        for registered in self.registry.list_tools():
            if not self._is_exposed(registered):
                continue
            name = registered["name"]
            title = TOOL_TITLES.get(name, name)
            tool = {
                "name": self._external_name(name),
                "title": title,
                "description": self._description(registered),
                "inputSchema": self._input_schema(registered),
                "annotations": {"title": title, **self._annotations(registered)},
            }
            tools.append(tool)
        return sorted(tools, key=lambda tool: tool["name"])

    def call_tool(self, external_name, arguments):
        internal_name = self._internal_name(external_name)
        registered = self.registry.get_tool(internal_name)
        if not registered or not self._is_exposed(registered):
            return tool_result({"error": f"Tool '{external_name}' is not available."}, is_error=True)

        run = self._create_run(internal_name, arguments)
        ctx = PermissionContext(self.user, self.agent.name, self.company)
        gateway = ToolGateway(ctx, self.registry)

        try:
            if internal_name.startswith(ACTION_PREFIX):
                gateway.create_approval(internal_name, arguments, run.name, None, self.agent)
                handler_arguments = dict(arguments)
                handler_arguments["run"] = run.name
                result = self._execute_handler(registered, handler_arguments, ctx)
                status = "Waiting Approval"
            else:
                result = gateway.authorize_and_execute(
                    internal_name,
                    dict(arguments),
                    run_name=run.name,
                    agent_doc=self.agent,
                )
                status = "Completed"

            from ai_agent.runtime.masking import mask_tool_result

            # MCP clients are external models too: same masking as the agent.
            result = mask_tool_result(internal_name, arguments, result)
            is_error = isinstance(result, dict) and bool(result.get("error"))
            self._finish_run(run.name, status if not is_error else "Failed", result)
            return tool_result(result, is_error=is_error)
        except (ToolDenied, PermissionDenied, frappe.PermissionError) as exc:
            frappe.db.rollback()
            self._finish_run(run.name, "Failed", {"error": str(exc)})
            return tool_result({"error": str(exc)}, is_error=True)
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"MCP Tool Failed: {internal_name}")
            self._finish_run(run.name, "Failed", {"error": "Tool execution failed."})
            return tool_result({"error": "Tool execution failed."}, is_error=True)

    def _assert_access(self):
        if self.user in (None, "", "Guest"):
            raise MCPProtocolError(-32001, "Authentication required")
        if not cint(self.settings.get("enable_mcp")):
            raise MCPProtocolError(-32002, "MCP server is disabled")

        allowed_roles = {
            role.strip()
            for role in (
                self.settings.get("mcp_allowed_roles") or "AI MCP User\nSystem Manager"
            ).splitlines()
            if role.strip()
        }
        if allowed_roles and not allowed_roles.intersection(frappe.get_roles(self.user)):
            raise MCPProtocolError(-32003, "The authenticated user is not allowed to use MCP")
        self._check_rate_limit()

    def _check_rate_limit(self):
        limit = max(1, cint(self.settings.get("mcp_max_requests_per_minute")) or 60)
        window = int(time.time()) // 60
        key = frappe.cache.make_key(f"ai-agent-mcp-rate:{self.user}:{window}")
        count = frappe.cache.incrby(key, 1)
        if count == 1:
            frappe.cache.expire(key, 120)
        if count > limit:
            raise MCPProtocolError(-32000, "MCP request rate limit exceeded")

    def _get_agent(self):
        agent_name = self.settings.get("mcp_agent") or "ERP Analyst"
        if not frappe.db.exists("AI Agent", {"name": agent_name, "enabled": 1}):
            raise MCPProtocolError(-32002, "The configured MCP agent is missing or disabled")
        return frappe.get_doc("AI Agent", agent_name)

    def _action_requests_enabled(self):
        return bool(
            cint(self.settings.get("mcp_enable_action_requests"))
            and not cint(self.agent.get("read_only"))
            and cint(self.agent.get("allow_actions"))
        )

    def _is_exposed(self, tool):
        from ai_agent.runtime import policy

        if tool["name"].startswith("actions.") and (
            cint(self.agent.get("read_only")) or not cint(self.agent.get("allow_actions"))
        ):
            return False
        if tool["name"] in policy.SUBMIT_CANCEL_TOOLS and not policy.submit_cancel_allowed():
            return False
        if tool["name"] == "tools.request_more":
            return False
        side_effect = tool.get("side_effect") or "read_only"
        if side_effect in SAFE_SIDE_EFFECTS:
            return True
        return tool["name"].startswith(ACTION_PREFIX) and self._action_requests_enabled()

    def _create_run(self, tool_name, arguments):
        run = frappe.get_doc(
            {
                "doctype": "AI Run",
                "agent": self.agent.name,
                "user": self.user,
                "company": self.company,
                "prompt": f"MCP tool call: {tool_name}",
                "plan_summary": json.dumps(arguments, ensure_ascii=False, default=str)[:2000],
                "status": "Running",
                "started_at": frappe.utils.now_datetime(),
            }
        )
        run.insert(ignore_permissions=True)
        frappe.db.commit()
        return run

    def _finish_run(self, run_name, status, result):
        values = {
            "status": status,
            "completed_at": frappe.utils.now_datetime(),
            "tool_call_count": 1,
        }
        if status == "Failed":
            values["error_summary"] = str((result or {}).get("error") or "Tool execution failed")[:500]
        else:
            values["final_answer"] = json.dumps(result, ensure_ascii=False, default=str)[:10000]
        frappe.db.set_value("AI Run", run_name, values, update_modified=False)
        frappe.db.commit()

    @staticmethod
    def _execute_handler(tool, arguments, context):
        module_path, function_name = tool["handler"].rsplit(".", 1)
        handler = getattr(importlib.import_module(module_path), function_name)
        return handler(arguments, context)

    @staticmethod
    def _description(tool):
        description = tool.get("description") or tool["name"]
        note = MCP_DESCRIPTION_NOTES.get(tool["name"])
        return f"{description.rstrip('.')}. {note}" if note else description

    @staticmethod
    def _external_name(name):
        return name.replace(".", "__")

    @staticmethod
    def _internal_name(name):
        return name.replace("__", ".")

    @staticmethod
    def _input_schema(tool):
        schema = copy.deepcopy(tool.get("parameters") or {"type": "object", "properties": {}})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        schema.setdefault("additionalProperties", False)
        return schema

    @staticmethod
    def _annotations(tool):
        side_effect = tool.get("side_effect") or "read_only"
        read_only = side_effect in SAFE_SIDE_EFFECTS
        return {
            "readOnlyHint": read_only,
            "destructiveHint": side_effect == "destructive",
            "idempotentHint": read_only,
            "openWorldHint": False,
        }
