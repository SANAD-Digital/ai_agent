"""Bridge MCP tools to AI Agent's existing permission-aware tool gateway."""

import copy
import importlib
import json
import time

import frappe
from frappe.utils import cint

from ai_agent.mcp import approvals, continuation, diagnostics, events, hosts, paging, refs
from ai_agent.mcp.protocol import MCPProtocolError, tool_result
from ai_agent.presentation import errors as friendly_errors
from ai_agent.presentation import present, schema, text as presentation_text
from ai_agent.presentation.i18n import t
from ai_agent.presentation.registry import registered_tools
from ai_agent.runtime.exceptions import PermissionDenied, ToolDenied
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.tool_gateway import ToolGateway
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.tool_registry import ToolRegistry


ACTION_PREFIX = "actions.request_"
SAFE_SIDE_EFFECTS = {"read_only", "prepare_only"}

# Human-readable titles for MCP clients that show them instead of tool names (English; translated by _title()).
TOOL_TITLES = {
    "actions.request_bulk_create": "Request several drafts (needs approval)",
    "actions.request_create_draft": "Request a draft (needs approval)",
    "actions.request_mapped_draft": "Request a draft from a document (needs approval)",
    "actions.request_update": "Request a document edit (needs approval)",
    "actions.request_workflow_action": "Request a workflow action (needs approval)",
    "actions.validate_document": "Validate document data",
    "artifacts.create_chart": "Create a chart",
    "artifacts.create_document_links": "Create document links",
    "artifacts.create_table": "Create a table",
    "attachments.list": "List attachments",
    "data.count": "Count records",
    "data.get_child_records": "Read child table rows",
    "data.get_document": "Read a document",
    "data.query": "Query data",
    "diagnose.document": "Diagnose a document",
    "erp.exchange_rate": "Exchange rate",
    "erp.fiscal_year": "Fiscal year",
    "erp.invoice_payments": "Invoice payments",
    "erp.item_details": "Item details",
    "erp.party_details": "Customer/supplier details",
    "erp.stock_balance": "Stock balance",
    "knowledge.search": "Search the knowledge base",
    "memory.search": "Search memory",
    "metrics.query": "Query a metric",
    "reports.describe": "Describe a report",
    "reports.run": "Run a report",
    "reports.search": "Find a report",
    "schema.describe_doctype": "Describe a document type",
    "schema.describe_links": "Document type links",
    "schema.describe_workflow": "Document type workflow",
    "workflow.get_transitions": "Workflow actions I can take on a document",
    "schema.search_doctypes": "Find document types",
    "setup.check": "Check setup",
    "skills.list": "List skills",
    "skills.load": "Load a skill",
    "skills.read_reference": "Read a skill reference",
    "approvals__status": "Approval request status",
    "approvals__decide": "Approve or reject a request",
    "approvals__revise": "Edit a request before approval (from the card)",
    "view__page": "Page, sort or search a table (from the card)",
    "view__event": "View diagnostics (from the card)",
    "view__load": "Load the full view (from the card)",
    "approvals__list_pending": "Pending approval requests",
    "app__probe": "Check the MCP App connection",
    "app__choose": "Choose from a list",
    "analysis.sales": "Sales analysis",
}


def _title(name):
    """A tool's title in the viewer's language (MCP hosts show it to people)."""
    return t(TOOL_TITLES[name]) if name in TOOL_TITLES else name


THEN_SCHEMA = {
    "type": "object",
    "description": "Optional: the user's ORIGINAL goal to continue automatically once this request is approved and executed "
    "(for example: create the customer and item now, `then` the sales invoice). Placeholders `$created.<DocType>` "
    "(e.g. \"$created.Customer\", \"$created.Item\") are replaced by the names of the records this request creates.",
    "properties": {
        "goal": {"type": "string", "maxLength": 300, "description": "One sentence, e.g. 'إنشاء فاتورة بيع نقدية لحمود صالح'"},
        "tool": {"type": "string", "enum": ["actions__request_create_draft", "actions__request_bulk_create"]},
        "arguments": {"type": "object", "description": "Arguments for that tool, with $created placeholders where the new records go"},
    },
    "required": ["goal", "tool", "arguments"],
}

# Tools the embedded app calls itself, or that complete the approval lifecycle for the model.
SYNTHETIC_TOOLS = {
    "app__choose": {
        "description": "Ask the user to pick one option instead of guessing (for example which company or warehouse). "
        "Use source=companies or source=warehouses to list what the user can read, or pass options. In hosts with MCP Apps the user sees a "
        "choice card and the answer arrives as their next message; elsewhere the same numbered list is in the text: ask them to reply.",
        "properties": {"source": {"type": "string", "enum": ["companies", "warehouses"]}, "title": {"type": "string", "maxLength": 120},
                       "subtitle": {"type": "string", "maxLength": 200}, "reply_template": {"type": "string", "maxLength": 120},
                       "options": {"type": "array", "maxItems": 20, "items": {"type": "object", "properties": {"value": {"type": "string"}, "label": {"type": "string"}, "description": {"type": "string"}}, "required": ["label"]}}},
        "required": [], "app_only": False, "read_only": True,
    },
    "app__probe": {
        "description": "Read-only connection check for the MCP App view: returns the number of companies the user can read. "
        "Renders a minimal view in hosts that support MCP Apps; plain text and structuredContent elsewhere.",
        "properties": {}, "required": [], "app_only": False, "read_only": True, "resource": "minimal",
    },
    "approvals__status": {
        "description": "Read the current state of an approval request (pending, executed, rejected, expired, failed) and its result.",
        "properties": {"approval_id": {"type": "string"}}, "required": ["approval_id"], "app_only": False, "read_only": True,
    },
    "approvals__list_pending": {
        "description": "The user's own pending approval requests. Call it when the user says 'موافق' / 'اعتمد' / 'نفذ' / 'yes go ahead' without naming a "
        "request: one pending request → decide on it; several → the result asks the user which one; none → say so.",
        "properties": {}, "required": [], "app_only": False, "read_only": True,
    },
    "approvals__decide": {
        "description": "Approve or reject a pending approval request. Use it when the user explicitly approves in chat ('موافق', 'اعتمد', 'نعم نفذ') "
        "or rejects ('ألغِ', 'لا'). Approving executes the action, verifies the created/changed document and returns its link; "
        "when the request carried a next step (`then`) the server prepares it and returns that next request too. Never call it "
        "without an explicit decision from the user. The card's buttons call the same tool with a signed token.",
        "properties": {"approval_id": {"type": "string"}, "decision": {"type": "string", "enum": ["approve", "reject"]},
                       "reason": {"type": "string", "maxLength": 300}, "token": {"type": "string", "description": "Set by the approval card only; leave empty from chat."}},
        "required": ["approval_id", "decision"], "app_only": False, "read_only": False, "destructive": True,
    },
    "approvals__revise": {
        "description": "App-only. Change quantity, price, company or warehouse of a pending create request before approval.",
        "properties": {"approval_id": {"type": "string"}, "token": {"type": "string"}, "changes": {"type": "object"}},
        "required": ["approval_id", "token", "changes"], "app_only": True, "read_only": False,
    },
    "view__event": {
        "description": "App-only. The embedded view reports its lifecycle (handshake, result received, render) for diagnostics.",
        "properties": {"view": {"type": "string", "maxLength": 40},
                       "events": {"type": "array", "maxItems": 12, "items": {"type": "object", "properties": {
                           "event": {"type": "string", "enum": sorted(events.VIEW_EVENTS)}, "detail": {"type": "string", "maxLength": 200}}, "required": ["event"]}}},
        "required": ["events"], "app_only": True, "read_only": True,
    },
    "view__load": {
        "description": "App-only. The embedded view loads its complete view model (rows, chart, action arguments) from the reference in structuredContent.",
        "properties": {"ref": {"type": "string"}}, "required": ["ref"], "app_only": True, "read_only": True,
    },
    "view__page": {
        "description": "App-only. Load another page, sort order or search of a table shown by a previous result.",
        "properties": {"token": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                       "sort_key": {"type": "string"}, "sort_dir": {"type": "string", "enum": ["asc", "desc"]},
                       "search_key": {"type": "string"}, "search": {"type": "string"},
                       "filters": {"type": "array", "maxItems": 8, "items": {"type": "object", "properties": {"field": {"type": "string"}, "operator": {"type": "string", "enum": ["=", "!=", ">=", "<=", "like", "in"]}, "value": {}}, "required": ["field", "operator", "value"]}}},
        "required": ["token"], "app_only": True, "read_only": True,
    },
}
# MCP clients cannot render artifacts inline; tell the model what to do instead.
MCP_DESCRIPTION_NOTES = {
    "artifacts.create_chart": (
        "Returns chart_url: a link that opens the drawn chart in the browser."
        " Always give chart_url to the user as a clickable link."
    ),
}


# Only the final result gets a view (table, chart, approval card). Lookups made while the
# model works stay plain text, so the host does not draw a card for every intermediate step.
VIEW_TOOLS = {
    "artifacts.create_table",
    "artifacts.create_chart",
    "actions.request_bulk_create",
    "actions.request_cancel",
    "actions.request_create_draft",
    "actions.request_mapped_draft",
    "actions.request_submit",
    "actions.request_update",
    "actions.request_workflow_action",
    "customize.request_field",
    "customize.request_properties",
    "customize.request_client_script",
    "customize.request_server_script",
    "customize.request_report",
    "customize.request_undo",
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
            "documents directly; they create a pending approval. Lookup tools (queries, reports, "
            "documents) are your working steps and are not shown to the user as views. "
            + self._final_result_instructions() +
            "When an approval is pending, the user may click the card's button OR approve in chat: if they say "
            "'موافق' / 'اعتمد' / 'نفذ', call approvals__list_pending (or approvals__decide with the approval_id you "
            "just received) and approve; never approve without the user's explicit word. approvals__decide "
            "executes, verifies and returns the document link. If the request needs prerequisite records "
            "(a customer or item that does not exist), put them in ONE bulk request, and once the user "
            "approves it continue with the main document (for example the invoice) in the same task "
            "instead of presenting the prerequisites as the final result. Ask with app__choose when "
            "a choice is ambiguous (which company, which warehouse) instead of guessing."
        ) + self._memory_instructions()

    def _final_result_instructions(self):
        if self.ui_supported is False:
            # This host shows no views: the reply itself carries the result.
            return ("Give the final answer in your reply as a short markdown table. For a trend or "
                    "comparison also call artifacts__create_chart and give the user its chart_url as a "
                    "link. For a pending approval, summarise it and ask the user to reply 'موافق'. ")
        return ("When you have the final answer, show it once with artifacts__create_table (rows) or "
                "artifacts__create_chart (trend or comparison); the host renders that view, so do not "
                "re-print the table or repeat its numbers, add analysis and the next step instead. ")

    def _memory_instructions(self):
        """What the user's Desk conversations taught the assistant, so every MCP host
        (ChatGPT, Claude, Cline) starts from the same preferences. Preferences only —
        they are few and short; facts stay behind memory__search."""
        try:
            from ai_agent.engine import memory as memory_mod

            if not memory_mod.enabled(self.agent, self.settings):
                return ""
            rows = frappe.get_all(
                "AI Memory",
                filters={"user": self.user, "memory_type": "Preference"},
                fields=["content", "expires_at"],
                order_by="modified desc",
                limit=memory_mod.MAX_PREFERENCES,
            )
            now = frappe.utils.now_datetime()
            lines = [f"- {memory_mod._line(frappe._dict(r))}" for r in rows if not (r.expires_at and r.expires_at < now)]
            facts = frappe.db.count("AI Memory", {"user": self.user, "memory_type": ("!=", "Preference")})
        except Exception:
            return ""
        out = ""
        if lines:
            out += "\n\nThis user's remembered preferences (apply them):\n" + "\n".join(lines)
        if facts:
            out += f"\n\n{facts} remembered facts about this user's work (customers, mappings, habits) are available: call memory__search with the entity or topic before asking the user something they may have told you before."
        return out

    def list_tools(self):
        tools = []
        for registered in self.registry.list_tools():
            if not self._is_exposed(registered):
                continue
            name = registered["name"]
            title = _title(name)
            tool = {
                "name": self._external_name(name),
                "title": title,
                "description": self._description(registered),
                "inputSchema": self._input_schema(registered),
                "annotations": {"title": title, **self._annotations(registered)},
            }
            meta = self._tool_meta(name)
            if meta:
                tool["_meta"] = meta
            tools.append(tool)
        tools += self._synthetic_tools()
        events.log("tool_registered", user=self.user, count=len(tools), with_ui=sum(1 for tl in tools if "_meta" in tl and tl["_meta"].get("ui", {}).get("resourceUri")))
        return sorted(tools, key=lambda tool: tool["name"])

    def _tool_meta(self, internal_name):
        if self.ui_supported is False:
            return None
        if internal_name in VIEW_TOOLS and internal_name in registered_tools():
            return hosts.tool_ui_meta()
        return None

    def _synthetic_tools(self):
        if not self._action_requests_enabled():
            available = {k: SYNTHETIC_TOOLS[k] for k in ("app__probe", "app__choose", "view__page", "view__load", "view__event")}
        elif not approvals.app_approval_enabled():
            available = {k: v for k, v in SYNTHETIC_TOOLS.items() if not v["app_only"] or k in ("view__page", "view__load", "view__event")}
        else:
            available = SYNTHETIC_TOOLS
        text_only = self.ui_supported is False
        out = []
        for name, spec in available.items():
            if text_only and spec["app_only"]:
                continue   # only a rendered view can call these
            tool = {
                "name": name, "title": _title(name), "description": spec["description"],
                "inputSchema": {"type": "object", "properties": spec["properties"], "required": spec["required"], "additionalProperties": False},
                "annotations": {"title": _title(name), "readOnlyHint": spec["read_only"], "destructiveHint": bool(spec.get("destructive")),
                                "idempotentHint": spec["read_only"], "openWorldHint": False},
            }
            if not text_only:
                tool["_meta"] = hosts.tool_ui_meta(app_only=spec["app_only"], with_view=not spec["app_only"],
                                                   resource_uri=hosts.minimal_uri() if spec.get("resource") == "minimal" else None)
            out.append(tool)
        return out

    def note_request(self, method, params):
        try:
            diagnostics.note_request(self.user, method, params if isinstance(params, dict) else {}, dict(frappe.request.headers) if getattr(frappe, "request", None) else {})
        except Exception:
            pass   # diagnostics must never break a request

    def note_resource_requested(self, uri):
        events.log("ui_resource_requested", user=self.user, uri=uri)

    def note_resource_failed(self, uri, error):
        events.log("ui_resource_failed", user=self.user, uri=uri, error=str(error)[:200])

    def note_resource_read(self, uri):
        events.log("ui_resource_loaded", user=self.user, uri=uri)
        try:
            diagnostics.note_resource_read(self.user, uri)
        except Exception:
            pass

    def set_client_capabilities(self, capabilities, *, remember=False):
        """Host capability detection, best evidence first.

        1. The host declared MCP Apps (``extensions["io.modelcontextprotocol/ui"]``): supported.
        2. The host already fetched one of our UI resources: it renders views, whatever it declared.
        3. The host declared capabilities without MCP Apps: not supported (clean text fallback).
        4. Nothing known yet: unknown (views are offered, approval buttons wait for evidence).
        """
        declared = hosts.ui_supported(capabilities)
        key = frappe.cache.make_key(f"ai-agent-mcp-ui:{self.user}")
        if declared is None:
            raw = frappe.cache.get_value(key)
            declared = None if raw is None else bool(raw)
        elif remember:
            frappe.cache.set_value(key, 1 if declared else 0, expires_in_sec=86400)
        supported = True if declared or diagnostics.render_seen(self.user) else declared
        if hosts.text_only_host(events.host_name()):
            supported = False
        self.ui_supported = supported
        frappe.flags.mcp_ui_supported = supported

    def call_tool(self, external_name, arguments):
        if external_name in SYNTHETIC_TOOLS:
            return self._call_synthetic(external_name, arguments)
        internal_name = self._internal_name(external_name)
        registered = self.registry.get_tool(internal_name)
        if not registered or not self._is_exposed(registered):
            return self._error(f"Tool '{external_name}' is not available.")

        run = self._create_run(internal_name, arguments)
        ctx = PermissionContext(self.user, self.agent.name, self.company)
        # Customization gates: the MCP surface, and every MCP request counts as tainted (the host reads
        # the web and files with its own tools, out of our sight).
        ctx.surface = "MCP"
        ctx.tainted = True
        gateway = ToolGateway(ctx, self.registry)

        then = None
        try:
            if registered.get("requires_approval"):
                then = arguments.pop("then", None)
                if then is not None:
                    then = continuation.clean(then)
                gateway.create_approval(internal_name, arguments, run.name, None, self.agent)
                handler_arguments = dict(arguments)
                handler_arguments["run"] = run.name
                result = self._execute_handler(registered, handler_arguments, ctx)
                status = "Waiting Approval"
                if isinstance(result, dict) and result.get("approval_id"):
                    events.log("approval_created", user=self.user, tool=internal_name, approval_id=result["approval_id"], doctype=result.get("doctype"))
                    if then:
                        continuation.store(result["approval_id"], self.user, then)
                        events.log("continuation_stored", user=self.user, approval_id=result["approval_id"], goal=then["goal"])
                    if not approvals.approval_required() and not self._always_human(result["approval_id"]):
                        return self._direct_execute(result["approval_id"], internal_name, run.name)
            else:
                events.log("tool_called", user=self.user, tool=internal_name)
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
            if is_error:
                return self._error(result.get("error"), internal_name)
            envelope = present(internal_name, arguments, result, ctx)
            if envelope:
                return self._envelope_result(envelope, internal_name, arguments)
            return tool_result(result)
        except continuation.ContinuationError as exc:
            self._finish_run(run.name, "Failed", {"error": str(exc)})
            return self._error(str(exc))
        except (ToolDenied, PermissionDenied, frappe.PermissionError) as exc:
            frappe.db.rollback()
            self._finish_run(run.name, "Failed", {"error": str(exc)})
            return self._error(str(exc))
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"MCP Tool Failed: {internal_name}")
            self._finish_run(run.name, "Failed", {"error": "Tool execution failed."})
            return self._error("Tool execution failed.")

    ui_supported = None

    def _envelope_result(self, envelope, internal_name, arguments, *, for_app=False):
        """One contract for every presented tool: data / summary / presentation / actions / warnings / meta.

        content            text fallback, complete on its own
        structuredContent  what the model needs: data preview, summary, a lean presentation (no rows) and `ref`
        _meta              the full view model for hosts that hand `_meta` to the view
        Results of calls made by the view itself (`for_app`) carry the full view model in structuredContent, because
        that is the one channel every MCP Apps host is documented to deliver to the view.
        """
        presentation = envelope.get("presentation")
        if presentation and (presentation.get("paging") or {}).get("pageable") and internal_name in paging.PAGEABLE_TOOLS:
            presentation["paging"]["token"] = paging.issue(internal_name, arguments, self.user)
            presentation["paging"]["search_key"] = next(
                (c["key"] for c in presentation.get("columns", []) if c["type"] == "text"), None)
        if presentation and for_app:
            shown = {**presentation, "full": True}
        else:
            shown = schema.lite(presentation)
            ref = self._ref(presentation, internal_name, arguments)
            if ref and isinstance(shown, dict):
                shown["ref"] = ref
        structured = {**envelope, "presentation": shown}
        rows_in_text = 40 if self.ui_supported is False else 15
        text = presentation_text.render(presentation, (envelope.get("summary") or {}).get("text"), max_rows=rows_in_text)
        result = {
            "content": [{"type": "text", "text": text or json.dumps(envelope["summary"], ensure_ascii=False, default=str)}],
            "structuredContent": structured,
            "isError": False,
        }
        if presentation and self.ui_supported is not False:
            result["_meta"] = {"ai_agent/presentation": presentation}
        else:
            events.log("fallback_used", user=self.user, tool=internal_name, reason="host declared no MCP Apps support" if presentation else "tool has no view")
        return result

    def _error(self, message, tool=None):
        """An error as the user should read it (title, reason, what to do); the raw text stays available to the model."""
        view = friendly_errors.friendly(message, tool)
        presentation = {"type": "error", "title": view["title"], "reason": view["reason"], "suggestion": view["suggestion"], "retry": view["retry"],
                        "kind": view["kind"], "warnings": [], "actions": [], "insights": [], "developer": {"error": view["raw"], "tool": tool}}
        result = {
            "content": [{"type": "text", "text": friendly_errors.text_of(view)}],
            "structuredContent": {"error": view["raw"], "error_view": {k: view[k] for k in ("kind", "title", "reason", "suggestion", "retry")},
                                  "presentation": {k: v for k, v in presentation.items() if k != "developer"}},
            "isError": True,
        }
        if self.ui_supported is not False:
            result["_meta"] = {"ai_agent/presentation": presentation}
        return result

    def _ref(self, presentation, internal_name, arguments):
        if not isinstance(presentation, dict) or self.ui_supported is False:
            return None
        if presentation.get("type") == "approval" and presentation.get("approval_id"):
            return refs.issue("approval", {"id": presentation["approval_id"]}, self.user)
        tool = self.registry.get_tool(internal_name) if internal_name else None
        if tool and (tool.get("side_effect") or "read_only") in SAFE_SIDE_EFFECTS and presentation.get("type") in ("table", "document"):
            return refs.issue("tool", {"t": internal_name, "a": arguments}, self.user)
        return None

    def _load(self, arguments):
        """view__load: rebuild the full view model a reference names (an approval, or a read-only tool call repeated)."""
        kind, payload = refs.read(arguments.get("ref"), self.user)
        if kind == "approval":
            return approvals.status(self.user, payload["id"]), "approvals.status", {}
        tool, args = payload["t"], payload.get("a") or {}
        registered = self.registry.get_tool(tool)
        if not registered or not self._is_exposed(registered) or (registered.get("side_effect") or "read_only") not in SAFE_SIDE_EFFECTS:
            raise approvals.ApprovalUiError(t("This view reference is not valid."))
        run = self._create_run(tool, args)
        ctx = PermissionContext(self.user, self.agent.name, self.company)
        result = ToolGateway(ctx, self.registry).authorize_and_execute(tool, dict(args), run_name=run.name, agent_doc=self.agent)
        from ai_agent.runtime.masking import mask_tool_result

        result = mask_tool_result(tool, args, result)
        if isinstance(result, dict) and result.get("error"):
            self._finish_run(run.name, "Failed", result)
            raise approvals.ApprovalUiError(str(result["error"]))
        self._finish_run(run.name, "Completed", result)
        envelope = present(tool, args, result, ctx)
        if not envelope:
            raise approvals.ApprovalUiError(t("Could not build the view."))
        return envelope, tool, args

    def _call_synthetic(self, name, arguments):
        try:
            if name == "app__probe":
                return self._probe()
            if name == "view__event":
                for item in (arguments.get("events") or [])[:12]:
                    if isinstance(item, dict) and item.get("event") in events.VIEW_EVENTS:
                        events.log(item["event"], user=self.user, detail=str(item.get("detail") or "")[:200], view=str(arguments.get("view") or "")[:40])
                try:
                    diagnostics.note_resource_read(self.user, "rendered:" + str(arguments.get("view") or "view")[:40])   # proof the host renders views
                except Exception:
                    pass
                return {"content": [{"type": "text", "text": "ok"}], "structuredContent": {"ok": True}, "isError": False}
            if name == "app__choose":
                return self._envelope_result(self._choose(arguments), "app.choose", {})
            if name == "approvals__status":
                envelope = approvals.status(self.user, arguments.get("approval_id"))
            elif name == "approvals__list_pending":
                envelope = approvals.list_pending(self.user)
            elif name == "approvals__decide":
                if arguments.get("decision") != "reject":
                    self._assert_decidable_here(arguments.get("approval_id"))
                envelope = approvals.decide(self.user, arguments.get("approval_id"), arguments.get("decision"),
                                            arguments.get("token") or None, arguments.get("reason"))
                envelope = self._continue_after(envelope, arguments.get("approval_id"))
            elif name == "approvals__revise":
                self._assert_decidable_here(arguments.get("approval_id"))
                envelope = approvals.revise(self.user, arguments.get("approval_id"), arguments.get("token"), arguments.get("changes"))
            elif name == "view__load":
                envelope, tool, load_args = self._load(arguments)
                return self._envelope_result(envelope, tool, load_args, for_app=True)
            else:
                envelope, tool, page_args = self._page(arguments)
                return self._envelope_result(envelope, tool, page_args, for_app=True)
            # A call that carries the card's token was made by the view: it gets the full view model back.
            by_app = name == "approvals__revise" or bool(arguments.get("token"))
            return self._envelope_result(envelope, envelope["meta"].get("tool", name), {}, for_app=by_app)
        except approvals.ApprovalUiError as exc:
            return self._error(str(exc))
        except (ToolDenied, PermissionDenied, frappe.PermissionError) as exc:
            frappe.db.rollback()
            return self._error(str(exc))
        except Exception:
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), f"MCP Synthetic Tool Failed: {name}")
            return self._error("Tool execution failed.")

    def _choose(self, arguments):
        from ai_agent.presentation.presenters import choice

        source = arguments.get("source")
        if source:
            if source not in choice.SOURCES:
                raise approvals.ApprovalUiError(t("Unknown source."))
            spec, options = choice.options_from_source(source, self.user)
            title, subtitle, icon, reply = spec["title"], spec["subtitle"], spec["icon"], spec["reply"]
        else:
            options = choice.clean_options(arguments.get("options"))
            title, subtitle, icon, reply = t("Choose an option"), None, "check", t("I chose: {label}")
        if not options:
            raise approvals.ApprovalUiError(t("There are no options to choose from."))
        return choice.build(arguments.get("title") or title, options, subtitle=arguments.get("subtitle") or subtitle, icon=icon,
                            reply_template=arguments.get("reply_template") or reply)

    @staticmethod
    def _always_human(approval_id):
        from ai_agent.runtime import policy

        return policy.requires_human(frappe.db.get_value("AI Approval Request", approval_id, "action_type"))

    def _assert_decidable_here(self, approval_id):
        """A Customize request is approved from Desk only (D4): never by approvals__decide, which an external
        model calls, nor by the card's buttons. Rejecting from here stays allowed."""
        if not approval_id:
            return
        action_type = frappe.db.get_value("AI Approval Request", approval_id, "action_type")
        if action_type == "Customize":
            url = frappe.utils.get_url(f"/app/ai-approval-request/{approval_id}")
            raise PermissionDenied(
                f"Customization changes are approved in ERPNext only, not from an external AI tool. Open {url} to review "
                "and approve it, or ask the user to approve it in the ERPNext chat."
            )

    def _direct_execute(self, approval_id, internal_name, run_name):
        """Approval not required: run the requester's own request now, verify, and continue its `then`."""
        events.log("approval_skipped", user=self.user, tool=internal_name, approval_id=approval_id, reason="mcp_require_approval=0")
        engine = ApprovalEngine(PermissionContext(self.user))
        approval = frappe.get_doc("AI Approval Request", approval_id)
        try:
            events.log("approval_direct_execute", user=self.user, approval_id=approval_id, doctype=approval.doctype_name)
            result = engine.execute_direct(approval_id, self.user)
        except Exception as exc:
            frappe.db.rollback()
            events.log("approval_failed", user=self.user, approval_id=approval_id, error=str(exc)[:200])
            self._finish_run(run_name, "Failed", {"error": str(exc)})
            approval.reload()
            return self._envelope_result(approvals.card_envelope(approval, self.user, tool=internal_name, state="failed", error=approvals._plain_error(exc)), internal_name, {})
        approval.reload()
        verified = approvals.record_verification(self.user, approval, result, via="direct", mode="direct")
        approval.reload()
        self._finish_run(run_name, "Completed", result)
        envelope = approvals.card_envelope(approval, self.user, tool=internal_name, state="verified" if verified else "executed", result=result)
        envelope["data"]["approval_required"] = False
        return self._envelope_result(self._continue_after(envelope, approval_id), internal_name, {})

    def _continue_after(self, envelope, approval_id):
        """After an executed request, prepare the stored next step (the user's original goal) and attach it."""
        state = (envelope.get("summary") or {}).get("state")
        if state not in ("executed", "verified") or not approval_id:
            return envelope
        then = continuation.take(approval_id)
        if not then:
            return envelope
        events.log("continuation_started", user=self.user, approval_id=approval_id, goal=then["goal"], tool=then["tool"])
        created = continuation.created_documents((envelope.get("data") or {}).get("result") or {})
        try:
            args = continuation.resolve(then["arguments"], created)
            next_result = self.call_tool(then["tool"], dict(args))
        except continuation.ContinuationError as exc:
            events.log("continuation_failed", user=self.user, approval_id=approval_id, error=str(exc)[:200])
            envelope["warnings"].append({"level": "warning", "text": t("Could not continue with the original goal ({0}): {1}", then["goal"], exc), "code": "continuation"})
            envelope["summary"]["text"] += " " + t("Automatic follow-up failed: {0}", exc)
            return envelope
        nxt = next_result.get("structuredContent") or {}
        next_text = (nxt.get("summary") or {}).get("text") or "".join(c.get("text", "") for c in next_result.get("content") or [] if c.get("type") == "text")
        if next_result.get("isError"):
            events.log("continuation_failed", user=self.user, approval_id=approval_id, error=str(next_text)[:200])
            envelope["warnings"].append({"level": "warning", "text": t("Could not continue with the original goal ({0}): {1}", then["goal"], next_text), "code": "continuation"})
            envelope["summary"]["text"] += " " + t("Automatic follow-up failed: {0}", next_text)
            return envelope
        events.log("continuation_completed", user=self.user, approval_id=approval_id, next_approval=(nxt.get("data") or {}).get("approval_id"))
        # The next step's card replaces the finished one; the finished step is summarised in a line above it.
        done_text = envelope["summary"]["text"]
        merged = next_result.get("_meta", {}).get("ai_agent/presentation") or nxt.get("presentation")
        out = {**nxt}
        out["summary"] = {**(nxt.get("summary") or {}), "text": t("{0} Then we continued with the original goal ({1}): {2}", done_text, then["goal"], (nxt.get("summary") or {}).get("text", ""))}
        out["data"] = {**(nxt.get("data") or {}), "previous_step": envelope.get("data")}
        out["meta"] = {**(nxt.get("meta") or {}), "continued_from": approval_id, "goal": then["goal"]}
        out["presentation"] = merged
        if merged and isinstance(merged, dict):
            merged.setdefault("insights", []).insert(0, t("Previous step done: {0}", done_text))
        out["warnings"] = list(nxt.get("warnings") or [])
        out["actions"] = list(nxt.get("actions") or [])
        return out

    def _probe(self):
        """Smallest possible tool result: structuredContent + a plain sentence that stands alone without any UI."""
        count = len(frappe.get_list("Company", pluck="name", user=self.user, limit_page_length=0))
        return {
            "content": [{"type": "text", "text": t("Companies: {0}", count)}],
            "structuredContent": {"companies": count, "user": self.user},
            "isError": False,
        }

    def _page(self, arguments):
        tool, original = paging.read(arguments.get("token"), self.user)
        args = paging.page_arguments(
            original, offset=arguments.get("offset"), limit=arguments.get("limit"), sort_key=arguments.get("sort_key"),
            sort_dir=arguments.get("sort_dir"), search_key=arguments.get("search_key"), search=arguments.get("search"), filters=arguments.get("filters"))
        run = self._create_run(tool, args)
        ctx = PermissionContext(self.user, self.agent.name, self.company)
        result = ToolGateway(ctx, self.registry).authorize_and_execute(tool, dict(args), run_name=run.name, agent_doc=self.agent)
        from ai_agent.runtime.masking import mask_tool_result

        result = mask_tool_result(tool, args, result)
        if isinstance(result, dict) and result.get("error"):
            self._finish_run(run.name, "Failed", result)
            raise approvals.ApprovalUiError(str(result["error"]))
        self._finish_run(run.name, "Completed", result)
        envelope = present(tool, args, result, ctx)
        if not envelope:
            raise approvals.ApprovalUiError(t("Could not show this page."))
        envelope["meta"]["paged"] = True
        return envelope, tool, args

    def _assert_access(self):
        if self.user in (None, "", "Guest"):
            raise MCPProtocolError(-32001, "Authentication required")
        if not cint(self.settings.get("enable_mcp")):
            raise MCPProtocolError(-32002, "MCP server is disabled")

        from ai_agent.mcp.setup import allowed_roles as mcp_allowed_roles

        allowed_roles = set(mcp_allowed_roles(self.settings))
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

        if (tool["name"].startswith("actions.") or tool["name"].startswith("customize.")) and (
            cint(self.agent.get("read_only")) or not cint(self.agent.get("allow_actions"))
        ):
            return False
        if tool["name"].startswith("customize."):
            # Off unless AI Settings allows customization over MCP (customization_allow_mcp) and the user holds
            # the roles; requests also need action requests enabled for MCP. Approval is from Desk only.
            from ai_agent.customization import policy as customization_policy

            if not customization_policy.tool_available(tool["name"], self.user, customization_policy.MCP):
                return False
            if tool.get("requires_approval") and not self._action_requests_enabled():
                return False
        if tool["name"] in policy.SUBMIT_CANCEL_TOOLS and not policy.submit_cancel_allowed():
            return False
        if tool["name"] in ("tools.request_more", "files.read"):   # files.read reads Desk chat attachments only
            return False
        if tool["name"].startswith("web."):   # MCP hosts browse with their own tools; no server-side fetching for external callers
            return False
        from ai_agent.runtime import access

        if not access.tool_allowed(tool["name"]):   # a plugin's tool: AI Settings access rules
            return False
        side_effect = tool.get("side_effect") or "read_only"
        if side_effect in SAFE_SIDE_EFFECTS:
            return True
        if tool["name"].startswith("customize."):
            return True   # checked above
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
        if tool["name"] in ("actions.request_bulk_create", "actions.request_create_draft"):
            schema["properties"]["then"] = THEN_SCHEMA
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
