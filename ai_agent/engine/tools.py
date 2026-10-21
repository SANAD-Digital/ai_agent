"""Tool execution for the loop, on top of the existing (kept) gateway:
permission checks, call budgets, argument validation, audit rows and the
approval engine all stay where they are. This module only decides which
tools the model sees and turns gateway results into blocks.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import frappe

from ai_agent.engine.types import (
    APPROVAL,
    ARTIFACT,
    MEMORY,
    QUESTION,
    SKILL,
    SUGGESTIONS,
    TOOL_RESULT,
    Block,
    ToolSpec,
    clip,
    registry_name,
    wire_name,
)
from ai_agent.runtime import access, policy
from ai_agent.runtime.action_interceptor import KNOWN_ACTION_TOOLS, ActionInterceptor
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied, SkillDenied, SkillNotFound, ToolDenied
from ai_agent.runtime.masking import mask_tool_result
from ai_agent.runtime.result_compaction import compact_tool_result_for_model
from ai_agent.runtime.tool_gateway import ToolGateway
from ai_agent.runtime.tool_registry import ToolRegistry
from ai_agent.runtime.untrusted import wrap

DEFAULT_MODEL_RESULT_CHARS = 4000
UI_RESULT_MAX_ROWS = 100
UI_RESULT_MAX_CHARS = 60_000
HIDDEN_TOOLS = {"tools.request_more"}       # the old keyword-routing crutch; replaced by tools.enable

# Progressive tool loading: the core set covers most requests; a group is added
# only when the model asks for it (tools__enable). Loading a skill never changes
# the list: the tool schemas sit at the start of every request, so changing them
# throws away the provider's prompt cache (measured 2026-10-08: a skill that
# added two erp.* tools turned ~15k cached input tokens into full-price ones).
CORE_TOOLS = {
    "skills.list", "skills.load", "skills.read_reference",
    "schema.search_doctypes", "schema.describe_doctype",
    "data.query", "data.get_document", "data.count",
    "metrics.query", "reports.search", "reports.describe", "reports.run", "analysis.sales",
    "artifacts.create_table", "artifacts.create_chart",
    "memory.search", "memory.store_context", "files.read",
    "actions.prepare_document", "actions.validate_document", "actions.request_create_draft", "actions.request_update",
    # plugin tools: always in the list (hidden per user by the access rules), never added mid-conversation
    "export.excel", "export.csv", "export.pdf", "export.image", "export.print", "export.statement", "messaging.request_email",
}
def _customize_tools():
    from ai_agent.customization import registry

    return (*registry.READ_TOOLS, *registry.REQUEST_TOOLS, registry.UNDO_TOOL)


TOOL_GROUPS = {
    "erp_details": {
        "description": "ERPNext lookups for building transactions: item price/stock/accounts, party account/currency/terms, fiscal year, exchange rate, stock balance, payments against an invoice",
        "tools": ("erp.item_details", "erp.party_details", "erp.fiscal_year", "erp.exchange_rate", "erp.stock_balance", "erp.invoice_payments"),
    },
    "diagnostics": {
        "description": "Why a document is missing from a ledger/statement, setup checks for an ERP area, link and workflow structure of a DocType",
        "tools": ("diagnose.document", "setup.check", "schema.describe_links", "schema.describe_workflow"),
    },
    "knowledge": {
        "description": "Search the user guides for how-to answers with citations",
        "tools": ("knowledge.search",),
    },
    "documents": {
        "description": "Child-table rows of a document, attachment metadata, document-link artifacts",
        "tools": ("data.get_child_records", "attachments.list", "artifacts.create_document_links"),
    },
    "web": {
        "description": "Search the internet and read public web pages or PDFs (documentation, prices, news, regulations)",
        "tools": ("web.search", "web.fetch"),
    },
    "actions_more": {
        "description": "Further approval-gated actions: bulk create, create from an existing document by ERPNext mapping, submit, cancel",
        "tools": ("actions.request_bulk_create", "actions.request_mapped_draft", "actions.request_submit", "actions.request_cancel"),
    },
    "workflow": {
        "description": "Document workflow: the state of a document and the workflow actions this user may take (approve, reject, send for review…), and requesting one",
        "tools": ("workflow.get_transitions", "actions.request_workflow_action"),
    },
    "customization": {
        "description": "Change how the system works: custom fields, field and form properties, list columns and filters, naming, client and server scripts (also scheduled and permission scripts), reports (Builder, Query, Python), print formats, notifications, workflows, number cards, charts and workspaces, web pages and forms, role permissions and new DocTypes (each change needs approval)",
        # the registry's tools, so a new kind needs no change here
        "tools": _customize_tools(),
    },
}
# Results that bring outside text into the conversation: a customization requested after one of
# these carries context.tainted (the card warns that the request may come from that content).
TAINTING_TOOLS = {"web.fetch", "web.search"}
# Chat-only tools that draw UI instead of reading data (Desk page and support window).
UI_SURFACES = ("Desk", "Chatbot")
ASK_TOOL = ToolSpec(
    name="ui__ask",
    description=(
        "Ask the user to choose, when the decision is theirs and you cannot infer it (which company, which "
        "approach, which of several matches). Shows a card with 2-4 options, each with a short description, plus "
        "\"Other\" for a free answer. Your reply ends right after this call; the answer arrives as the user's next "
        "message. Prefer it to asking a multiple-choice question in prose."
    ),
    parameters={"type": "object", "properties": {
        "question": {"type": "string", "description": "The question, one short sentence ending with ?"},
        "options": {"type": "array", "minItems": 2, "maxItems": 4, "items": {"type": "object", "properties": {
            "label": {"type": "string", "description": "1-5 words"},
            "description": {"type": "string", "description": "What this option means or implies"},
        }, "required": ["label"]}},
        "multiple": {"type": "boolean", "description": "Allow picking several options"},
    }, "required": ["question", "options"]},
)
SUGGEST_TOOL = ToolSpec(
    name="ui__suggest",
    description=(
        "After a complete answer, offer 2-3 short follow-up requests the user is likely to send next (in their "
        "language, as they would type them). Shown as chips under your answer; the reply ends with this call."
    ),
    parameters={"type": "object", "properties": {
        "suggestions": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string"}},
    }, "required": ["suggestions"]},
)

ENABLE_TOOL = ToolSpec(
    name="tools__enable",
    description="Enable an additional tool group for this conversation. Groups: " + ", ".join(f"{k} ({v['description']})" for k, v in TOOL_GROUPS.items()),
    parameters={"type": "object", "properties": {"group": {"type": "string", "enum": list(TOOL_GROUPS)}}, "required": ["group"]},
)


def drop_nulls(value):
    """Strict tool calling sends null for every optional field the model leaves
    out; the handlers expect such fields to be absent."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value


class _NoTrace:
    def __getattr__(self, _name):
        return lambda *a, **k: None


@dataclass
class ToolOutcome:
    result: Block | None = None             # tool_result block (None while an approval is pending)
    extras: list = field(default_factory=list)   # skill / artifact / memory blocks for the UI and the history
    approval: str = ""                      # AI Approval Request name when the call needs a decision
    approval_block: Block | None = None
    duration_ms: int = 0
    tool_call: str = ""                     # audit row
    end_turn: bool = False                  # ui.ask / ui.suggest: the reply stops here


class ToolExecutor:
    def __init__(self, ctx, agent_doc, run_name, settings=None, session=None):
        self.ctx = ctx
        self.agent = agent_doc
        self.run_name = run_name
        self.session = session  # AI Chat Session: its approval_mode can run a request at once
        self.settings = settings or frappe.get_cached_doc("AI Settings")
        self.registry = ToolRegistry()
        self.gateway = ToolGateway(ctx, self.registry, skill_tools=[])
        self.approvals = ApprovalEngine(ctx)
        self.interceptor = ActionInterceptor(
            run_name=run_name,
            agent=agent_doc,
            permission_context=ctx,
            approval_engine=self.approvals,
            # The chat engine records its own blocks; the interceptor's legacy
            # step/trace hooks are no-ops here (never None: it calls them).
            add_step=lambda *a, **k: frappe._dict(name=None),
            update_status=lambda *a, **k: None,
            trace=_NoTrace(),
        )
        self.surface = self._session_surface()
        self.tainted = False
        # customization gates read these from the permission context (gateway and customization.tools)
        self.ctx.surface = self.surface
        self.ctx.tainted = False
        self.loaded_skills = {}
        self.enabled = set(CORE_TOOLS)
        self.enabled_groups = set()
        self.specs_changed = False

    def enable_group(self, group):
        if group not in TOOL_GROUPS or group in self.enabled_groups:
            return False
        self.enabled_groups.add(group)
        self.enabled.update(TOOL_GROUPS[group]["tools"])
        self.specs_changed = True
        return True

    def groups_for_tools(self, rows):
        """Groups holding the skill's tools that the model does not have yet."""
        missing = {registry_name(r.get("tool_name") or "") for r in rows} - self.enabled
        return sorted(group for group, spec in TOOL_GROUPS.items() if group not in self.enabled_groups and missing & set(spec["tools"]))

    def group_available(self, group):
        return any(self.allowed(n) for n in TOOL_GROUPS[group]["tools"])

    def _unavailable_reason(self, group):
        """Say why a group is closed, so the model tells the user the real fix instead of "turned off"."""
        tools = TOOL_GROUPS[group]["tools"]
        writes = any(n.startswith(("customize.", "actions.")) for n in tools)
        if writes and (self.agent.get("read_only") or not self.agent.get("allow_actions")):
            return (f"Tool group '{group}' changes the system, and this assistant ({self.agent.get('name')}) is read-only. "
                    "Tell the user to start the conversation with an assistant that may act (allow actions in AI Agent).")
        if group == "customization" and not self.agent.get("allow_customization"):
            return (f"Customization belongs to the Implementation assistant, not {self.agent.get('name')}. "
                    "Tell the user to start a new conversation with the Implementation assistant for this change.")
        if group == "customization":
            from ai_agent.customization import policy as customize_policy

            if not customize_policy.enabled():
                return "Customization through the assistant is turned off in AI Settings (an administrator can turn it on)."
            return ("Customization is on, but this user lacks the role it needs (System Manager; Script Manager for "
                    "server-side code) or this channel may not request it.")
        return f"Tool group '{group}' is turned off on this site."

    def groups_for_prompt(self):
        return [{"name": k, "description": v["description"]} for k, v in TOOL_GROUPS.items()
                if k not in self.enabled_groups and self.group_available(k)]

    # -- what the model sees ---------------------------------------------------

    def allowed(self, name):
        if name in HIDDEN_TOOLS:
            return False
        if name.startswith("actions."):
            if self.agent.get("read_only") or not self.agent.get("allow_actions"):
                return False
            if name in policy.SUBMIT_CANCEL_TOOLS and not policy.submit_cancel_allowed():
                return False
        if name == "workflow.get_transitions" and (self.agent.get("read_only") or not self.agent.get("allow_actions")):
            return False
        if name.startswith("memory.") and (self.agent.get("disable_memory") or not self.settings.get("enable_memory")):
            return False
        if name.startswith("web.") and not self.settings.get("enable_web_browsing"):
            return False
        if name.startswith("customize."):
            if self.agent.get("read_only") or not self.agent.get("allow_actions") or not self.agent.get("allow_customization"):
                return False
            from ai_agent.customization import policy as customization_policy

            if not customization_policy.tool_available(name, self.ctx.user, self.surface):
                return False
        # a plugin's tool needs that plugin allowed for this user (AI Settings access rules)
        if not access.tool_allowed(name, self.ctx.user):
            return False
        return True

    def specs(self):
        out = []
        for tool in self.registry.list_tools():
            name = tool["name"]
            if name not in self.enabled or not self.allowed(name):
                continue
            out.append(ToolSpec(
                name=wire_name(name),
                description=tool.get("description") or name,
                parameters=tool.get("parameters") or {"type": "object", "properties": {}},
                read_only=tool.get("side_effect") in ("read_only", "prepare_only"),
            ))
        if len(self.enabled_groups) < len(TOOL_GROUPS):
            out.append(ENABLE_TOOL)
        if self._ui_surface():
            out += [ASK_TOOL, SUGGEST_TOOL]
        return out

    def _ui_surface(self):
        if not self.session:
            return False
        return (frappe.db.get_value("AI Chat Session", self.session, "surface") or "Desk") in UI_SURFACES

    def _session_surface(self):
        """Desk / Chatbot / WhatsApp / MCP / API, or None outside a conversation (customization refuses None)."""
        if not self.session:
            return None
        return frappe.db.get_value("AI Chat Session", self.session, "surface") or "Desk"

    def _note_taint(self, name, args, result):
        """Untrusted outside text entered the conversation: web pages, or a file that is not this chat's own upload."""
        if name in TAINTING_TOOLS:
            self.tainted = True
        elif name == "files.read" and args.get("file") and isinstance(result, dict) and not result.get("error"):
            try:
                from ai_agent.tools.chat_files import _attachments

                own = {f["file"] for f in _attachments(self.session)} if self.session else set()
            except Exception:
                own = set()
            if result.get("file") not in own:
                self.tainted = True
        self.ctx.tainted = self.tainted

    def _run_tainted(self):
        """A resumed reply has a new executor: the run's earlier calls count too."""
        if self.tainted or not self.run_name:
            return self.tainted
        try:
            self.tainted = bool(frappe.db.exists("AI Tool Call", {"run": self.run_name, "tool_name": ("in", list(TAINTING_TOOLS))}))
        except Exception:
            pass
        self.ctx.tainted = self.tainted
        return self.tainted

    def is_read_only(self, wire):
        tool = self.registry.get_tool(registry_name(wire)) or {}
        return tool.get("side_effect") in ("read_only", "prepare_only")

    # -- execution ----------------------------------------------------------------

    def execute(self, block: Block) -> ToolOutcome:
        name = registry_name(block.name)
        args = block.data if isinstance(block.data, dict) else {}
        args = drop_nulls({k: v for k, v in args.items() if not str(k).startswith("_")})
        started = time.monotonic()
        frappe.db.savepoint("ai_tool_call")
        try:
            if name not in ("tools.enable", "ui.ask", "ui.suggest") and not access.tool_allowed(name, self.ctx.user):
                raise ToolDenied(f"Tool '{name}' belongs to a plugin this user is not allowed to use.")
            if name == "tools.enable":
                outcome = self._enable_call(block, args)
            elif name in ("ui.ask", "ui.suggest"):
                outcome = self._ui_call(block, name, args)
            elif name in KNOWN_ACTION_TOOLS:
                outcome = self._approval_call(block, name, args)
            else:
                outcome = self._plain_call(block, name, args)
        except (ToolDenied, PermissionDenied, SkillDenied, SkillNotFound) as exc:
            outcome = ToolOutcome(result=self._error_block(block, str(exc)))
        except Exception as exc:
            # Half-done writes of a crashed tool must not ride along with the runner's next commit.
            try:
                frappe.db.rollback(save_point="ai_tool_call")
            except Exception:
                pass   # the tool committed already (approval requests do): nothing left to undo
            frappe.log_error(frappe.get_traceback(), f"AI tool {name} (message {getattr(self, 'message_name', '') or self.run_name or ''})")
            outcome = ToolOutcome(result=self._error_block(block, f"The tool failed: {frappe.utils.cstr(exc)[:300]}"))
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    def _ui_call(self, block, name, args):
        """Draw a question card or follow-up chips; the reply ends after it."""
        def clip_text(value, limit):
            return " ".join(str(value or "").split())[:limit]

        if name == "ui.ask":
            options = [{"label": clip_text(o.get("label"), 60), "description": clip_text(o.get("description"), 200)}
                       for o in (args.get("options") or []) if isinstance(o, dict) and clip_text(o.get("label"), 60)][:4]
            question = clip_text(args.get("question"), 300)
            if not question or len(options) < 2:
                return ToolOutcome(result=self._error_block(block, "ui__ask needs a question and 2-4 options with labels."))
            card = Block(kind=QUESTION, id=block.id, name=block.name, data={"question": question, "options": options, "multiple": bool(args.get("multiple"))})
            note = "The question is shown with its options. Stop here: the user's choice arrives as their next message."
        else:
            items = [clip_text(s, 90) for s in (args.get("suggestions") or []) if clip_text(s, 90)][:3]
            if not items:
                return ToolOutcome(result=self._error_block(block, "ui__suggest needs 1-3 suggestions."))
            card = Block(kind=SUGGESTIONS, id=block.id, name=block.name, data={"items": items})
            note = "Shown."
        text = wrap(json.dumps({"shown": True, "note": note}, ensure_ascii=False), f"tool {block.name}")
        return ToolOutcome(result=Block(kind=TOOL_RESULT, id=block.id, name=block.name, text=text, data={"shown": True}),
                           extras=[card], end_turn=True)

    def _enable_call(self, block, args):
        group = str(args.get("group") or "")
        if group not in TOOL_GROUPS:
            return ToolOutcome(result=self._error_block(block, f"Unknown tool group '{group}'. Groups: {', '.join(TOOL_GROUPS)}"))
        if not self.group_available(group):
            return ToolOutcome(result=self._error_block(block, self._unavailable_reason(group)))
        fresh = self.enable_group(group)
        names = [wire_name(n) for n in TOOL_GROUPS[group]["tools"] if self.allowed(n)]
        result = {"enabled": group, "tools": names, "note": "These tools are available from your next step." if fresh else "Group was already enabled."}
        return ToolOutcome(result=Block(kind=TOOL_RESULT, id=block.id, name=block.name, text=json.dumps(result, ensure_ascii=False), data=result))

    def _plain_call(self, block, name, args):
        result = self.gateway.authorize_and_execute(name, args, run_name=self.run_name, step_name=None, agent_doc=self.agent)
        result = mask_tool_result(name, args, result)
        self._note_taint(name, args, result)
        outcome = ToolOutcome()
        outcome.tool_call = self._latest_audit(name)
        if not isinstance(result, dict):
            result = {"result": result}

        if name == "skills.load" and not result.get("error"):
            skill_name = result.get("name") or args.get("skill_name")
            self.loaded_skills[skill_name] = result
            for row in result.get("tools") or []:
                if row.get("tool_name") and row not in self.gateway.skill_tools:
                    self.gateway.skill_tools.append(row)
            outcome.extras.append(Block(kind=SKILL, name=skill_name, text=skill_text(result, self.groups_for_tools(result.get("tools") or [])), data={"title": result.get("title"), "description": result.get("description"), "tools": [r.get("tool_name") for r in result.get("tools") or []]}))
            outcome.result = self._result_block(block, name, {"loaded": skill_name, "note": "The skill instructions follow as a separate message; apply them."}, ui_data={"loaded": skill_name})
            return outcome

        if name.startswith("artifacts.") and result.get("artifact_name"):
            outcome.extras.append(artifact_block(result["artifact_name"]))

        if name.startswith("export.") and result.get("file_url"):
            outcome.extras.append(file_block(result))

        if name == "memory.store_context" and result.get("stored"):
            outcome.extras.append(Block(kind=MEMORY, text=result.get("content_preview") or "", data={"memory": result.get("memory")}))

        if result.get("error"):
            outcome.result = self._error_block(block, str(result["error"]), data=result)
            return outcome

        outcome.result = self._result_block(block, name, result)
        view = self._view(name, args, result)
        if view:
            outcome.result.data = {"view": view, **(outcome.result.data if isinstance(outcome.result.data, dict) else {})}
            kind = view["structuredContent"]["presentation"].get("type")
            # Same rule MCP gives ChatGPT/Claude: the user already sees this result rendered.
            outcome.result.text += f"\n[The user sees this result as an interactive {kind} (search, sort, paging, links). Do not re-print its rows; give the answer, the analysis and the next step.]"
        return outcome

    def _view(self, name, args, result):
        """The same view model MCP hands to ChatGPT/Claude (presentation layer),
        so the Desk chat renders the identical MCP App for this result."""
        try:
            from ai_agent.chat.view_host import view_result

            return view_result(name, args, result, self.ctx, self.agent)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI chat view for {name}")
            return None

    def _approval_call(self, block, name, args):
        # Gateway validates, budgets and audits ("Requested"); the interceptor creates the approval.
        if name.startswith("customize."):
            self._run_tainted()
        self.gateway.create_approval(name, args, run_name=self.run_name, step_name=None, agent_doc=self.agent)
        response = self.interceptor.handle_action_tool(name, args)
        outcome = ToolOutcome(tool_call=self._latest_audit(name))
        if not isinstance(response, dict) or response.get("error") or not response.get("approval_id"):
            message = (response or {}).get("error") if isinstance(response, dict) else "approval could not be created"
            outcome.result = self._error_block(block, str(message or "approval could not be created"))
            return outcome
        approval = frappe.get_doc("AI Approval Request", response["approval_id"])
        if not policy.requires_human(approval.action_type) and self._auto_mode() and policy.auto_approves("Auto", approval.action_type):
            return self._auto_execute(block, name, approval, outcome)
        outcome.approval = approval.name
        outcome.approval_block = Block(kind=APPROVAL, id=block.id, name=name, data=approval_snapshot(approval))
        return outcome

    def _auto_mode(self):
        # read each time: the user may switch the mode while the reply runs
        if not self.session or not frappe.db.has_column("AI Chat Session", "approval_mode"):  # site not migrated yet
            return False
        return frappe.db.get_value("AI Chat Session", self.session, "approval_mode") == "Auto"

    def _auto_execute(self, block, name, approval, outcome):
        """Auto mode: the requester's own request runs at once (same checks and executor as an
        approved one, under the user's permissions). The decided card is kept for the record and
        the model gets the result, so the reply goes on without a pause."""
        result, error = None, None
        try:
            result = self.approvals.execute_direct(approval.name, self.ctx.user)
        except Exception as exc:
            error = frappe.utils.cstr(exc)[:500]
        approval.reload()
        snapshot = approval_snapshot(approval)
        snapshot.update({"auto": True, "decided_by": self.ctx.user, "result": _jsonable(result), "error": error})
        card = Block(kind=APPROVAL, id=block.id, name=name, data=snapshot)
        outcome.extras.append(card)
        note = (
            "Auto-approval is on in this conversation: the action was executed at once. Tell the user what was created or changed (link the document)."
            if approval.status == "Executed" else
            "Auto-approval is on, but the action failed. Explain the error to the user and propose a fix."
        )
        payload = {"approval": approval.name, "status": approval.status, "result": snapshot["result"], "error": error, "note": note}
        text = wrap(json.dumps(payload, ensure_ascii=False, default=str), f"tool {name}")
        outcome.result = Block(kind=TOOL_RESULT, id=block.id, name=block.name, text=text,
            data={"approval": approval.name, "status": approval.status, "result": snapshot["result"], "error": error}, is_error=bool(error))
        return outcome

    # -- blocks ---------------------------------------------------------------------

    def _result_block(self, block, name, result, ui_data=None):
        tool = self.registry.get_tool(name) or {}
        limit = int(tool.get("model_result_max_chars") or DEFAULT_MODEL_RESULT_CHARS)
        for_model = compact_tool_result_for_model(name, result)
        text = wrap(clip(json.dumps(for_model, ensure_ascii=False, default=str), limit), f"tool {name}")
        return Block(kind=TOOL_RESULT, id=block.id, name=block.name, text=text, data=ui_data if ui_data is not None else ui_result(result))

    @staticmethod
    def _error_block(block, message, data=None):
        text = wrap(json.dumps({"error": message}, ensure_ascii=False), f"tool {block.name}")
        return Block(kind=TOOL_RESULT, id=block.id, name=block.name, text=text, data={"error": message, **({"detail": ui_result(data)} if data else {})}, is_error=True)

    def _latest_audit(self, name):
        try:
            return frappe.db.get_value("AI Tool Call", {"run": self.run_name, "tool_name": name}, "name", order_by="creation desc") or ""
        except Exception:
            return ""


# -- helpers ---------------------------------------------------------------------

def ui_result(result):
    """What the chat shows under a tool card: the result with long row lists capped."""
    if not isinstance(result, dict):
        return {"result": result}
    out = {}
    for key, value in result.items():
        if isinstance(value, list) and len(value) > UI_RESULT_MAX_ROWS:
            out[key] = value[:UI_RESULT_MAX_ROWS]
            out[f"{key}_truncated"] = len(value) - UI_RESULT_MAX_ROWS
        else:
            out[key] = value
    text = json.dumps(out, ensure_ascii=False, default=str)
    if len(text) > UI_RESULT_MAX_CHARS:
        return {"_preview": text[:UI_RESULT_MAX_CHARS], "_truncated": True}
    return out


def skill_text(skill, groups=()):
    title = skill.get("title") or skill.get("name")
    body = (skill.get("instructions") or "").strip()
    note = (
        f"\nSome tools this skill mentions are not in your list yet; only if you need one, call tools__enable with group {' / '.join(groups)} first."
        if groups else ""
    )
    return f"<<<SKILL name=\"{skill.get('name')}\" title=\"{title}\">>>\n{body}\n<<<END_SKILL>>>\nApply these skill instructions for the rest of this task.{note}"


def file_block(result):
    """A download card (an artifact block of type File) for a plugin's output."""
    return Block(kind=ARTIFACT, name="File", text=result.get("title") or result.get("file_name") or "", data={
        "artifact_type": "File",
        "title": result.get("title") or result.get("file_name"),
        "format": result.get("format"),
        "file_name": result.get("file_name"),
        "file_url": result.get("file_url"),
        "file_size": result.get("file_size"),
        "row_count": result.get("rows"),
        "image": bool(result.get("image")),
    })


def plugin_text(plugin):
    title = plugin.get("title") or plugin.get("name")
    body = (plugin.get("instructions") or "").strip()
    tool = wire_name(plugin.get("tool") or "")
    return (
        f"<<<PLUGIN name=\"{plugin.get('name')}\" title=\"{title}\">>>\n"
        f"The user picked this plugin for the request that follows: deliver the result through the `{tool}` tool.\n{body}\n<<<END_PLUGIN>>>"
    )


def _jsonable(value):
    try:
        return json.loads(json.dumps(value, default=str)) if value is not None else None
    except (TypeError, ValueError):
        return {"raw": str(value)}


def approval_snapshot(approval):
    try:
        payload = json.loads(approval.payload_json or "{}")
    except ValueError:
        payload = {}
    return {
        "approval": approval.name,
        "approval_id": approval.approval_id,
        "status": approval.status,
        "action_type": approval.action_type,
        "doctype_name": approval.doctype_name,
        "document_name": approval.document_name,
        "action_summary": approval.action_summary,
        "impact_summary": approval.impact_summary,
        "payload": payload,
        "expires_at": str(approval.expires_at or ""),
        "requested_by": approval.requested_by,
        **_customization_section(approval),
    }


def _customization_section(approval):
    """The card's "customization" part (customization.presenter, group C), only for Customize requests."""
    if approval.action_type != "Customize":
        return {}
    try:
        from ai_agent.customization import presenter

        return {"customization": presenter.section(approval, frappe.session.user)}
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"AI customization card {approval.name}")
        return {}


def artifact_block(artifact_name):
    doc = frappe.get_doc("AI Artifact", artifact_name)
    try:
        content = json.loads(doc.content_json or "{}")
    except ValueError:
        content = {}
    rows = content.get("rows") or []
    data = {
        "artifact": doc.name,
        "artifact_type": doc.artifact_type,
        "title": doc.title,
        "summary": doc.summary,
        "row_count": doc.row_count,
        "is_truncated": bool(doc.is_truncated) or len(rows) > 50,
        "columns": content.get("columns") or (list(rows[0].keys()) if rows else []),
        "rows": rows[:50],
    }
    if doc.artifact_type == "Chart":
        data["chart"] = content.get("chart") or content
    if doc.artifact_type == "Document Links":
        data["links"] = content.get("links") or content.get("documents") or rows
    return Block(kind=ARTIFACT, name=doc.artifact_type, text=doc.title or "", data=data)
