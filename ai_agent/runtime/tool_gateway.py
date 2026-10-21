import json
import hashlib
import frappe
from ai_agent.runtime.exceptions import ToolDenied
from ai_agent.runtime.trace_service import TraceService


def coerce_to_schema(schema, values):
    """Repair the two shapes models often send instead of the schema's, in place: an object or a list
    written as JSON text ("{...}", "[...]"), and one name where a list of names is expected
    ("Accounts User" for roles). Anything else is left for the tool to reject."""
    if not isinstance(schema, dict) or not isinstance(values, dict):
        return values
    for key, child in (schema.get("properties") or {}).items():
        value = values.get(key)
        if not isinstance(child, dict) or not isinstance(value, str):
            if isinstance(child, dict) and child.get("type") == "object" and isinstance(value, dict):
                coerce_to_schema(child, value)
            continue
        kind = child.get("type")
        text = value.strip()
        if kind == "object" and text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                values[key] = coerce_to_schema(child, parsed)
        elif kind == "array":
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except ValueError:
                    continue
                if isinstance(parsed, list):
                    values[key] = parsed
            elif text and (child.get("items") or {}).get("type") in (None, "string"):
                values[key] = [part.strip() for part in text.split(",") if part.strip()]
    return values


class ToolGateway:
    """Enforces policy and permissions before tool execution."""

    def __init__(self, permission_context, tool_registry, skill_tools=None):
        self.ctx = permission_context
        self.registry = tool_registry
        self.skill_tools = skill_tools or []
        self.enforce_skill_tools = False
        self.trace = TraceService()
        self._call_counts = {}

    def authorize_and_execute(self, tool_name, arguments, run_name=None, step_name=None, agent_doc=None):
        """Authorize a tool call and execute it."""
        tool_name = tool_name.replace("__", ".")
        if tool_name.startswith("repo."):
            tool_name = tool_name[5:]
        elif tool_name.startswith("tools."):
            tool_name = tool_name[6:]

        tool = self.registry.get_tool(tool_name)
        if not tool:
            raise ToolDenied(f"Tool '{tool_name}' is not registered.")

        if not self._is_tool_allowed_by_agent(tool_name, agent_doc):
            raise ToolDenied(f"Tool '{tool_name}' is not allowed by this agent.")

        self._assert_customization_available(tool_name)

        from ai_agent.runtime import access

        if not access.tool_allowed(tool_name, self.ctx.user):
            raise ToolDenied(f"Tool '{tool_name}' belongs to a plugin this user is not allowed to use.")

        if not self._is_tool_allowed_by_skill(tool_name):
            if tool_name not in (
                "skills.list", "skills.load", "skills.read_reference",
                "schema.search_doctypes", "schema.describe_doctype",
            ):
                raise ToolDenied(f"Tool '{tool_name}' is not allowed by the loaded skill.")

        self._check_call_limit(tool_name, agent_doc)

        self._apply_skill_argument_limits(tool_name, arguments)

        self._validate_arguments(tool_name, arguments)

        if tool.get("requires_approval"):
            raise ToolDenied(f"Tool '{tool_name}' requires approval - use the appropriate request_* tool.")

        # Count only calls that passed every check: a denied call must not
        # permanently burn the per-tool budget (lethal with limit-1 tools).
        self._count_call(tool_name)

        previous_run_name = getattr(self.ctx, "run_name", None)
        self.ctx.run_name = run_name
        try:
            result = self._execute_handler(tool, arguments)
        finally:
            self.ctx.run_name = previous_run_name

        max_bytes = int(tool.get("result_max_bytes") or 102400)
        result = self._truncate_result(result, max_bytes=max_bytes)

        self._audit_record(run_name, step_name, tool_name, arguments, result, "Executed")

        return result

    def create_approval(self, tool_name, arguments, run_name, step_name, agent_doc):
        """Create an approval request for a write action."""
        tool_name = tool_name.replace("__", ".")
        if tool_name.startswith("repo."):
            tool_name = tool_name[5:]
        elif tool_name.startswith("tools."):
            tool_name = tool_name[6:]

        tool = self.registry.get_tool(tool_name)
        if not tool:
            raise ToolDenied(f"Tool '{tool_name}' is not registered.")

        if not self._is_tool_allowed_by_agent(tool_name, agent_doc):
            raise ToolDenied(f"Tool '{tool_name}' is not allowed by this agent.")

        self._assert_customization_available(tool_name)

        from ai_agent.runtime import access

        if not access.tool_allowed(tool_name, self.ctx.user):
            raise ToolDenied(f"Tool '{tool_name}' belongs to a plugin this user is not allowed to use.")

        self._check_call_limit(tool_name, agent_doc)

        self._validate_arguments(tool_name, arguments)

        self._count_call(tool_name)

        self._audit_record(run_name, step_name, tool_name, arguments, None, "Requested")

        return {"approval_required": True, "tool_name": tool_name, "arguments": arguments}

    def _is_tool_allowed_by_agent(self, tool_name, agent_doc):
        if not agent_doc:
            return True
        # customize.* (reads included) belong to agents that may act: they lead to approvals that change the system.
        acting = tool_name.startswith("actions.") or tool_name.startswith("customize.")
        if agent_doc.get("read_only") and acting:
            return False
        if not agent_doc.get("allow_actions") and acting:
            return False
        if tool_name.startswith("customize.") and not agent_doc.get("allow_customization"):
            return False   # the Implementation agent (or one an admin opted in) owns customization
        return True

    def _assert_customization_available(self, tool_name):
        """customize.* need the AI Settings switches, Frappe's roles and an allowed surface. The caller sets
        ``ctx.surface`` ("Desk" chat, "MCP"); without it (WhatsApp, support bubble, legacy runs) they are refused."""
        if not tool_name.startswith("customize."):
            return
        from ai_agent.customization import policy as customization_policy

        if not customization_policy.tool_available(tool_name, self.ctx.user, getattr(self.ctx, "surface", None)):
            raise ToolDenied(f"Tool '{tool_name}' is not available: customization is turned off here or needs other roles.")

    def _is_tool_allowed_by_skill(self, tool_name):
        if not self.enforce_skill_tools:
            return True
        allowed_names = [t.get("tool_name") for t in self.skill_tools]
        return tool_name in allowed_names

    def _check_call_limit(self, tool_name, agent_doc):
        count = self._call_counts.get(tool_name, 0)
        max_calls = int(agent_doc.maximum_tool_calls) if agent_doc and agent_doc.get("maximum_tool_calls") else 25
        skill_tool = next(
            (row for row in self.skill_tools if row.get("tool_name") == tool_name),
            None,
        )
        if skill_tool and skill_tool.get("maximum_calls"):
            max_calls = min(max_calls, int(skill_tool.get("maximum_calls")))
        if count >= max_calls:
            raise ToolDenied(f"Tool '{tool_name}' has reached its call limit ({max_calls}).")

    def _count_call(self, tool_name):
        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1

    def _apply_skill_argument_limits(self, tool_name, arguments):
        skill_tool = next(
            (row for row in self.skill_tools if row.get("tool_name") == tool_name),
            None,
        )
        max_rows = int((skill_tool or {}).get("maximum_result_rows") or 0)
        if not max_rows or not isinstance(arguments, dict):
            return
        try:
            requested = int(arguments.get("limit") or max_rows)
        except (TypeError, ValueError):
            requested = max_rows
        arguments["limit"] = max(1, min(requested, max_rows))

    def _validate_arguments(self, tool_name, arguments):
        if not isinstance(arguments, dict):
            raise ToolDenied("Tool arguments must be a JSON object.")

        dangerous_keys = {"ignore_permissions", "user", "run_as", "commit"}
        overlap = self._find_dangerous_keys(arguments, dangerous_keys)
        if overlap:
            raise ToolDenied(f"Disallowed arguments: {', '.join(overlap)}")

        tool = self.registry.get_tool(tool_name) or {}
        schema = tool.get("parameters") or {}
        coerce_to_schema(schema, arguments)
        missing = self._missing_required_arguments(schema, arguments)
        if missing:
            raise ToolDenied(
                f"Tool '{tool_name}' missing required argument(s): {', '.join(missing)}."
            )

    def _missing_required_arguments(self, schema, values, prefix=""):
        if not isinstance(schema, dict) or not isinstance(values, dict):
            return []

        missing = []
        for key in schema.get("required") or []:
            value = values.get(key)
            if value in (None, ""):
                missing.append(f"{prefix}{key}")

        properties = schema.get("properties") or {}
        for key, child_schema in properties.items():
            if key not in values or values.get(key) is None:
                continue
            if isinstance(child_schema, dict) and child_schema.get("type") == "object":
                child_missing = self._missing_required_arguments(
                    child_schema,
                    values.get(key),
                    f"{prefix}{key}.",
                )
                missing.extend(child_missing)

        return missing

    def _find_dangerous_keys(self, value, dangerous_keys):
        found = set()
        if isinstance(value, dict):
            found.update(dangerous_keys.intersection(value.keys()))
            for child in value.values():
                found.update(self._find_dangerous_keys(child, dangerous_keys))
        elif isinstance(value, list):
            for child in value:
                found.update(self._find_dangerous_keys(child, dangerous_keys))
        return found

    def _execute_handler(self, tool, arguments):
        import importlib
        handler_path = tool["handler"]
        module_path, func_name = handler_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        handler = getattr(module, func_name)
        return handler(arguments, self.ctx)

    def _truncate_result(self, result, max_bytes=102400):
        if isinstance(result, dict):
            import json
            data = json.dumps(result, default=str)
            if len(data.encode("utf-8")) > max_bytes:
                result = {"truncated": True, "preview": data[:1000]}
        return result

    def _audit_record(self, run_name, step_name, tool_name, arguments, result, status):
        try:
            import json
            result_hash = ""
            result_preview = ""
            if result:
                result_json = json.dumps(result, sort_keys=True, default=str)
                result_hash = hashlib.sha256(result_json.encode()).hexdigest()
                result_preview = result_json[:2000]

            tool_call = frappe.get_doc({
                "doctype": "AI Tool Call",
                "run": run_name,
                "step": step_name,
                "call_id": f"{tool_name}-{frappe.utils.cint(frappe.utils.now_datetime().timestamp())}",
                "tool_name": tool_name,
                "arguments_json": json.dumps(arguments, default=str),
                "result_preview": result_preview,
                "result_hash": result_hash,
                "status": status,
                "started_at": frappe.utils.now_datetime(),
                "completed_at": frappe.utils.now_datetime(),
            })
            tool_call.insert(ignore_permissions=True)
        except Exception:
            # Fail closed: an unaudited tool call must not be reported as a success.
            try:
                frappe.log_error(frappe.get_traceback(), "AI Tool Call Audit Failed")
            except Exception:
                pass
            raise ToolDenied(
                f"Tool '{tool_name}' call could not be audited; the call was aborted."
            )
