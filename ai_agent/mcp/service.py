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
            tool = {
                "name": self._external_name(registered["name"]),
                "title": registered["name"],
                "description": registered.get("description") or registered["name"],
                "inputSchema": self._input_schema(registered),
                "annotations": self._annotations(registered),
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
        if tool["name"].startswith("actions.") and (
            cint(self.agent.get("read_only")) or not cint(self.agent.get("allow_actions"))
        ):
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
