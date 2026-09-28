import json
import hashlib
import frappe
from frappe.utils import cint
from ai_agent.runtime.context_builder import ContextBuilder
from ai_agent.runtime.model_provider import get_provider
from ai_agent.runtime.skill_registry import SkillRegistry
from ai_agent.runtime.tool_registry import ToolRegistry
from ai_agent.runtime.tool_gateway import ToolGateway
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.artifact_service import ArtifactService
from ai_agent.runtime.trace_service import TraceService
from ai_agent.runtime.memory_service import MemoryService
from ai_agent.runtime.file_context import FileContextService
from ai_agent.runtime.local_answers import greeting_or_capability_answer
from ai_agent.runtime import answer_contract, result_compaction, router, step_narration
from ai_agent.runtime.action_interceptor import ActionInterceptor
from ai_agent.runtime.run_control import run_timeout_seconds
from ai_agent.runtime.exceptions import (
    RunCancelled, RunTimedOut, RunLimitExceeded, MYSYSAgentError,
    ToolDenied, PermissionDenied,
)
from ai_agent.runtime.constants import (
    RUN_STATUS_PLANNING, RUN_STATUS_RUNNING,
    RUN_STATUS_COMPLETED, RUN_STATUS_FAILED,
    RUN_STATUS_CANCELLED, RUN_STATUS_TIMED_OUT,
    REALTIME_EVENTS,
)


class Orchestrator:
    """Main execution loop for AI Runs."""

    COMPACT_CHECKPOINT_START = "<!-- AI_AGENT_RUNTIME_CHECKPOINT_START -->"
    COMPACT_CHECKPOINT_END = "<!-- AI_AGENT_RUNTIME_CHECKPOINT_END -->"
    def __init__(self, run_name):
        self.run_name = run_name
        self.run = frappe.get_doc("AI Run", run_name)
        self.agent = frappe.get_doc("AI Agent", self.run.agent)
        self.ctx = PermissionContext(self.run.user, self.run.agent, self.run.company)
        self.skill_registry = SkillRegistry()
        self.tool_registry = ToolRegistry()
        self.trace = TraceService()
        self.artifacts = ArtifactService()
        self.approval_engine = ApprovalEngine(self.ctx)
        self.context_builder = ContextBuilder()
        self.memory = MemoryService()
        self.provider = get_provider(self.run.get("provider"))
        self.tool_gateway = ToolGateway(self.ctx, self.tool_registry)
        self.action_interceptor = ActionInterceptor(
            run_name=self.run_name,
            agent=self.agent,
            permission_context=self.ctx,
            approval_engine=self.approval_engine,
            add_step=self._add_step,
            update_status=self._update_status,
            trace=self.trace,
        )
        self.sequence = 0
        self.loaded_skills = []
        self.skill_tools = []
        self.effective_skills = []
        self.enforce_skill_tools = False
        self.input_tokens = cint(self.run.input_tokens)
        self.output_tokens = cint(self.run.output_tokens)
        self._timeout_seconds = None
        self.route = "open_analysis"
        self.extra_tools = []

    def execute(self):
        """Main execution entry point."""
        try:
            # Stamp actual execution start so queue wait time never counts
            # against the run's execution timeout.
            self.run.started_at = frappe.utils.now_datetime()
            frappe.db.set_value(
                "AI Run", self.run_name, "started_at", self.run.started_at, update_modified=False
            )
            self._update_status(RUN_STATUS_PLANNING)
            self._check_limits()

            effective_skills = self.skill_registry.list_effective_skills(
                self.run.user, self.run.agent, self.run.company
            )
            self._configure_skill_runtime(effective_skills)
            self._configure_route()

            plan_step = self._add_step("Plan", "Analyzing task and building plan")

            skill_metadata = self._select_context_skills(effective_skills)

            tool_schemas = self._get_tool_schemas()

            context = self.context_builder.build(
                self.run, self.agent, skill_metadata, tool_schemas, route=self.route
            )
            self.trace.complete_step(
                plan_step.name,
                summary=f"Loaded {len(skill_metadata)} skills and {len(tool_schemas)} tools.",
            )

            self._update_status(RUN_STATUS_RUNNING)
            self._update_compact_session_checkpoint(
                RUN_STATUS_RUNNING,
                f"Plan loaded with {len(skill_metadata)} skills and {len(tool_schemas)} tools.",
            )

            local_answer = self._try_local_fast_answer()
            if local_answer:
                final_step = self._add_step(
                    "Final",
                    "Answering locally",
                    summary=local_answer[:500],
                )
                self.trace.complete_step(final_step.name)
                self._complete_run(local_answer)
                return

            direct_answer = None
            # Routed questions are known to need ERP data: skip the preflight call.
            if self.route == "open_analysis" and not self.run.parent_run and not self._has_current_attachments():
                direct_answer = self._try_direct_answer_without_tools(context)
            if direct_answer:
                final_step = self._add_step(
                    "Final",
                    "Answering without tools",
                    summary=direct_answer[:500],
                )
                self.trace.complete_step(final_step.name)
                self._complete_run(direct_answer)
                return

            state = {
                "messages": context["messages"],
                "model": context["model"],
                "reasoning_effort": context.get("reasoning_effort"),
                "tool_call_count": 0,
                "step_count": 0,
                "empty_report_runs": [],
                "successful_report_runs": 0,
                "tool_observations": [],
                "tool_attempts": {},
            }

            result = self._run_loop(state)

            self._complete_run(result)

        except RunCancelled:
            self._update_status(RUN_STATUS_CANCELLED)
        except RunTimedOut as exc:
            self._time_out_run(str(exc))
        except RunLimitExceeded as e:
            self._fail_run(str(e))
        except MYSYSAgentError as e:
            self._fail_run(str(e))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Run Crashed: {self.run_name}")
            self._fail_run("An unexpected error occurred.")

    def _run_loop(self, state):
        """Main model loop."""
        max_steps = self.agent.maximum_steps or 12
        route_limit = router.ROUTE_STEP_LIMITS.get(self.route)
        if route_limit and self.route != "open_analysis":
            max_steps = min(max_steps, route_limit)
        max_tool_calls = self.agent.maximum_tool_calls or 25

        while state["step_count"] < max_steps:
            self._check_cancellation()

            state["step_count"] += 1
            # A routed question gets no tools on its last step, so the model
            # answers from what it has instead of hitting the limit mid-call.
            final_step = self.route != "open_analysis" and state["step_count"] == max_steps

            policy = self._current_skill_policy()
            result_compaction.compact_seen_tool_results(state["messages"])
            messages = state["messages"]
            if final_step:
                messages = messages + [{
                    "role": "system",
                    "content": (
                        "No more tools are available in this step. Answer the user's question now "
                        "from the tool results above; if data is missing or empty, say so plainly."
                    ),
                }]
            response = self._call_model_step(
                "Analyzing context and selecting the next action",
                instructions={
                    "messages": messages,
                    "model": state["model"],
                    "reasoning_effort": state.get("reasoning_effort"),
                    "max_tokens": policy.get("finalization_max_tokens") or None,
                    "response_format": (
                        {"type": "json_object"}
                        if policy.get("response_format") == "JSON Object"
                        else None
                    ),
                    "max_retries": 3,
                },
                input_items=[],
                tools=[] if final_step else self._get_tool_schemas(),
                state=state,
            )

            if not response:
                break

            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})

            if message.get("tool_calls"):
                tool_calls = message["tool_calls"]
                clean_message = {
                    "role": "assistant",
                    "content": message.get("content"),
                }
                if "reasoning_content" in message:
                    clean_message["reasoning_content"] = message.get("reasoning_content")
                if message.get("provider_blocks"):
                    # Anthropic: thinking blocks must be replayed unchanged with tool results.
                    clean_message["provider_blocks"] = message["provider_blocks"]
                clean_message["tool_calls"] = tool_calls
                state["messages"].append(clean_message)

                stop_reason = ""
                terminal_reason = ""
                empty_report_answer = None
                for tc in tool_calls:
                    if state["tool_call_count"] >= max_tool_calls:
                        state["messages"].append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": json.dumps({
                                "error": f"Tool call limit ({max_tool_calls}) reached.",
                            }),
                        })
                        stop_reason = (
                            f"Reached tool call limit ({max_tool_calls}) before the model produced a final answer."
                        )
                        continue

                    state["tool_call_count"] += 1

                    self._check_cancellation()

                    func = tc.get("function", {})
                    tool_name = func.get("name", "").replace("__", ".")
                    try:
                        arguments = json.loads(func.get("arguments", "{}"))
                    except json.JSONDecodeError:
                        arguments = {}
                    attempt_key = self._tool_attempt_key(tool_name, arguments)
                    state["tool_attempts"][attempt_key] = state["tool_attempts"].get(attempt_key, 0) + 1

                    step = self._add_step("Tool", step_narration.tool_step_title(tool_name, arguments))
                    self.trace.update_run_status(
                        self.run_name,
                        RUN_STATUS_RUNNING,
                        tool_call_count=state["tool_call_count"],
                        current_step=self.sequence,
                    )

                    try:
                        if tool_name == router.REQUEST_MORE:
                            result = self._handle_request_more(arguments)
                        elif tool_name.startswith("actions.") and "request" in tool_name:
                            # Gateway first: agent policy, call limits, argument
                            # validation, and the AI Tool Call audit record.
                            self.tool_gateway.create_approval(
                                tool_name, arguments, self.run_name, step.name, self.agent
                            )
                            result = self.action_interceptor.handle_action_tool(tool_name, arguments)
                        else:
                            result = self.tool_gateway.authorize_and_execute(
                                tool_name, arguments, self.run_name, step.name, self.agent
                            )
                    except (ToolDenied, PermissionDenied) as exc:
                        result = {"error": str(exc)}
                    self._track_report_result(state, tool_name, arguments, result)
                    self._track_metric_fallback(state, tool_name)
                    result = self.action_interceptor.maybe_create_approval_from_prepare(tool_name, result)
                    if tool_name == "skills.load" and isinstance(result, dict) and "error" not in result:
                        self.loaded_skills.append(result)
                        self.skill_tools = self._merge_skill_tools(
                            self.skill_tools,
                            result.get("tools") or [],
                        )
                        self.tool_gateway.skill_tools = self.skill_tools
                    tool_summary = step_narration.summarize_tool_result(tool_name, result, arguments)
                    self.trace.complete_step(
                        step.name,
                        summary=tool_summary,
                    )
                    self._update_compact_session_checkpoint(
                        RUN_STATUS_RUNNING,
                        tool_summary,
                        state=state,
                    )

                    model_result = result_compaction.compact_tool_result_for_model(tool_name, result)
                    self._record_tool_observation(state, tool_name, arguments, model_result)

                    # Every assistant tool_call_id must receive a tool response before
                    # another model request, even when execution should stop afterward.
                    state["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result_compaction.tool_result_content_for_model(self.tool_registry, tool_name, model_result),
                        # Private keys (stripped before sending) for compaction after use.
                        "_tool": tool_name,
                        "_summary": tool_summary[:500],
                    })

                    if state["tool_attempts"][attempt_key] >= 2:
                        stop_reason = (
                            f"Stopped because the same tool call repeated "
                            f"{state['tool_attempts'][attempt_key]} times."
                        )

                    if self._is_empty_report_loop(state):
                        empty_report_answer = step_narration.empty_report_loop_answer(state)

                    if self._is_terminal_tool_result(tool_name, model_result):
                        terminal_reason = (
                            f"Tool result from {tool_name} is sufficient or should be reported now."
                        )

                if empty_report_answer:
                    final_step = self._add_step(
                        "Final",
                        "Stopping repeated empty report attempts",
                        summary=empty_report_answer[:500],
                    )
                    self.trace.complete_step(final_step.name)
                    return empty_report_answer

                if stop_reason:
                    return self._complete_graceful_stop(state, stop_reason)

                if terminal_reason:
                    final_answer = self._try_final_answer_without_more_tools(state, terminal_reason)
                    if final_answer:
                        final_step = self._add_step(
                            "Final",
                            "Finalizing from tool result",
                            summary=final_answer[:500],
                        )
                        self.trace.complete_step(final_step.name)
                        return final_answer

            elif message.get("content") and not (
                final_step and not answer_contract.strip_tool_markup(message["content"]).strip()
            ):
                done_step = self._add_step("Final", "Generating final answer", summary=message["content"][:500])
                self.trace.complete_step(done_step.name)
                return message["content"]
            elif final_step:
                # The forced last step produced no usable text (e.g. tool-call markup).
                return self._complete_graceful_stop(state, "Routed step limit reached.")
            else:
                finish_reason = choice.get("finish_reason") or "unknown"
                final_answer = self._try_final_answer_without_more_tools(
                    state,
                    "The prior model response had no content or tool calls. "
                    "Produce the requested final answer now from the complete available evidence.",
                )
                if final_answer:
                    final_step = self._add_step(
                        "Final",
                        "Recovering final answer after an empty model response",
                        summary=final_answer[:500],
                    )
                    self.trace.complete_step(final_step.name)
                    return final_answer
                return self._complete_graceful_stop(
                    state,
                    f"The model returned no content or tool calls (finish reason: {finish_reason}).",
                )

        if state.get("last_successful_report"):
            final_answer = step_narration.answer_from_successful_report(state["last_successful_report"])
            final_step = self._add_step(
                "Final",
                "Summarizing successful report",
                summary=final_answer[:500],
            )
            self.trace.complete_step(final_step.name)
            return final_answer

        return self._complete_graceful_stop(
            state,
            "Reached step limit before the model produced a final answer.",
        )

    def _select_context_skills(self, effective_skills):
        priority = {
            "core-agent-behavior": 0,
            "whatsapp-business-support": 1,
            "compound-document-operations": 1,
            "document-creation": 2,
            "document-workflow": 3,
            "frappe-schema-discovery": 4,
            # Legacy purchase-invoice bundle skills (superseded by the OCR →
            # journal-entry pipeline): keep them last so they never evict the
            # analysis skills from the 12 context slots.
            "classify_purchase_invoice_attachments": 99,
            "analyze_purchase_invoice_bundle": 99,
            "match_purchase_invoice_supplier": 99,
            "match_purchase_invoice_items": 99,
            "validate_purchase_invoice_analysis": 99,
            "create_purchase_invoice_from_analysis": 99,
        }
        ordered = sorted(
            effective_skills or [],
            key=lambda skill: (priority.get(skill.get("name"), 50), skill.get("name") or ""),
        )
        return ordered[:12]

    def _try_local_fast_answer(self):
        if self._is_whatsapp_support_agent():
            return None
        if self.run.get("parent_run"):
            return None
        if self._has_current_attachments():
            return None
        return greeting_or_capability_answer(self.run.prompt)

    def _try_direct_answer_without_tools(self, context):
        if self._is_whatsapp_support_agent():
            return None

        user_message = ""
        for message in reversed(context.get("messages") or []):
            if message.get("role") == "user":
                user_message = message.get("content") or ""
                break

        if not user_message.strip():
            return None

        route_messages = [
            {
                "role": "system",
                "content": (
                    "You are a strict preflight router for an ERP assistant. "
                    "No tools are available in this step. "
                    "If the user is greeting, chatting, asking a general question, or asking something answerable without private ERP records, "
                    "return JSON: {\"route\":\"answer\",\"answer\":\"...\"}. "
                    "If the user request is missing required business information, return JSON: {\"route\":\"clarify\",\"answer\":\"one concise question\"}. "
                    "If the request needs live ERP records, reports, permissions, or a write action, return JSON: {\"route\":\"tools\",\"reason\":\"...\"}. "
                    "Never invent ERP data. Return only valid JSON."
                ),
            },
            {"role": "user", "content": user_message[:6000]},
        ]

        try:
            response = self._call_model_step(
                "Checking whether tools are required",
                instructions={
                    "messages": route_messages,
                    "model": context["model"],
                    "reasoning_effort": "low",
                    "max_tokens": 700,
                    "max_retries": 1,
                },
                input_items=[],
                tools=[],
                state={"route_only": True},
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Preflight Router Failed: {self.run_name}")
            return None

        route = answer_contract.parse_json_message_response(response)
        if route.get("route") in ("answer", "clarify") and route.get("answer"):
            return route.get("answer")
        return None

    def _is_whatsapp_support_agent(self):
        if self.agent.name == "WhatsApp Technical Support":
            return True
        if self.agent.get("agent_name") == "WhatsApp Technical Support":
            return True
        try:
            skills = self.skill_registry.list_effective_skills(
                self.run.user,
                self.run.agent,
                self.run.company,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Skill Listing Failed: {self.run_name}")
            return False
        return any(skill.get("name") == "whatsapp-business-support" for skill in skills or [])

    def _has_current_attachments(self):
        try:
            return bool(FileContextService().get_run_files(self.run_name))
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Attachment Check Failed: {self.run_name}")
            return False

    def _try_final_answer_without_more_tools(self, state, reason):
        messages = list(state.get("messages") or [])
        policy = self._current_skill_policy()
        requires_json = self._requires_json_final()
        output_instruction = policy.get("final_output_instruction") or (
            "Return one complete valid JSON object only, following the active skill instructions. "
            "Do not return Markdown, progress text, or a promise to continue. "
            if requires_json
            else "If the available result is enough, produce the final user-facing answer in concise Markdown. "
        )
        messages.append({
            "role": "system",
            "content": (
                "You have tool results available. Do not call tools in this step. "
                + output_instruction
                +
                "If the result is pending, empty after sensible attempts, approval-required, or blocked, report that status clearly. "
                "For compound tasks, do not imply the whole task is complete when only a prerequisite approval was created. "
                "State the remaining original task and tell the user they can continue after approval. "
                "Only if more tool data is truly required, return JSON: {\"route\":\"continue\",\"reason\":\"...\"}. "
                f"Reason to consider finalizing now: {reason}"
            ),
        })
        try:
            response = self._call_model_step(
                "Finalizing the answer from available evidence",
                    instructions={
                        "messages": messages,
                        "model": state["model"],
                        "reasoning_effort": "medium" if requires_json else "low",
                        "max_tokens": policy.get("finalization_max_tokens") or (4096 if requires_json else 1500),
                        "timeout": policy.get("finalization_timeout") or None,
                        "response_format": {"type": "json_object"} if requires_json else None,
                        "max_retries": 1,
                    },
                    input_items=[],
                    tools=[],
                    state=state,
                )
        except Exception as exc:
            state["finalization_error"] = str(exc)[:500]
            frappe.log_error(frappe.get_traceback(), f"AI Agent Finalization Failed: {self.run_name}")
            return None

        choice = response.get("choices", [{}])[0]
        content = (choice.get("message", {}) or {}).get("content") or ""
        if answer_contract.looks_like_tool_markup(content):
            # The model answered a no-tools finalization call with raw
            # tool-call markup: it still needs tools. Let the loop continue
            # instead of accepting the markup as a final answer.
            return None
        route = answer_contract.parse_json_text(content)
        if route.get("route") == "continue":
            return None
        if route.get("answer") and not requires_json:
            return route.get("answer")
        return content.strip() or None

    def _is_terminal_tool_result(self, tool_name, result):
        if not isinstance(result, dict):
            return False
        if result.get("error"):
            error_text = str(result.get("error", "")).lower()
            return any(
                marker in error_text
                for marker in ("not allowed", "denied", "permission", "requires approval", "call limit")
            )
        if result.get("approval_required"):
            return True
        if result.get("is_pending") or result.get("prepared_status") in ("Queued", "Started", "Missing"):
            return True
        if tool_name.startswith("actions.") and result.get("approval_id"):
            return True
        # "Positive" signals below say a read succeeded — under an unmet
        # compound JSON contract they must not force early finalization.
        if answer_contract.compound_contract_pending(self._current_skill_policy()):
            return False
        if result.get("terminal") is True or result.get("exhaustive") is True:
            return True
        if tool_name == "reports.run" and "row_count" in result:
            return cint(result.get("row_count")) > 0
        if tool_name == "actions.validate_document" and result.get("valid") is True:
            return True
        return False

    def _complete_graceful_stop(self, state, reason):
        final_answer = self._try_final_answer_without_more_tools(
            state,
            f"{reason} Use only the available tool results and produce a customer-facing final answer. Do not mention limits, tool names, debug details, or internal execution.",
        )
        if not final_answer:
            if self._requires_json_final():
                final_answer = json.dumps(
                    answer_contract.fallback_json_final_answer(self._current_skill_policy(), reason),
                    ensure_ascii=False,
                    default=str,
                )
            else:
                final_answer = (
                    "تم استلام طلبك، لكن لم أتمكن من تجهيز النتيجة النهائية بالكامل الآن. "
                    "سأتابع البيانات المتاحة وأعود لك بالرد المناسب."
                )
        final_step = self._add_step(
            "Final",
            "Graceful stop with available results",
            summary=(
                final_answer[:500]
                if not state.get("finalization_error")
                else f"{reason} Finalization error: {state.get('finalization_error')}"[:500]
            ),
        )
        self.trace.complete_step(final_step.name)
        self._update_compact_session_checkpoint(
            "Needs Review" if self._requires_json_final() else RUN_STATUS_COMPLETED,
            reason,
            state=state,
        )
        return final_answer

    def _tool_attempt_key(self, tool_name, arguments):
        return hashlib.sha256(
            json.dumps(
                {"tool": tool_name, "arguments": arguments},
                sort_keys=True,
                default=str,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    def _record_tool_observation(self, state, tool_name, arguments, result):
        observations = state.setdefault("tool_observations", [])
        observations.append({
            "tool": tool_name,
            "arguments": arguments,
            "result": result,
            "summary": step_narration.summarize_tool_result(tool_name, result, arguments),
        })
        if len(observations) > 12:
            del observations[:-12]

    def _track_report_result(self, state, tool_name, arguments, result):
        if tool_name != "reports.run" or not isinstance(result, dict) or result.get("error"):
            return

        if cint(result.get("row_count")) > 0:
            state["successful_report_runs"] = state.get("successful_report_runs", 0) + 1
            state["last_successful_report"] = result_compaction.compact_tool_result_for_model(tool_name, result)
            return

        state.setdefault("empty_report_runs", []).append({
            "report": result.get("report") or arguments.get("report_name"),
            "filters": result.get("filters") or arguments.get("filters") or {},
        })

    FALLBACK_TOOL_PREFIXES = ("schema.", "data.query", "data.count", "reports.run")

    def _track_metric_fallback(self, state, tool_name):
        """Record questions answered by exploration instead of the metric catalog."""
        if not tool_name.startswith(self.FALLBACK_TOOL_PREFIXES):
            return
        used = state.setdefault("fallback_tools", [])
        if tool_name in used:
            return
        used.append(tool_name)
        try:
            frappe.db.set_value(
                "AI Run", self.run_name,
                {"metric_fallback": 1, "fallback_tools": ", ".join(used)},
                update_modified=False,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Fallback Tracking Failed: {self.run_name}")

    def _is_empty_report_loop(self, state):
        if state.get("successful_report_runs"):
            return False
        empty_runs = state.get("empty_report_runs") or []
        if len(empty_runs) < 3:
            return False
        unique_attempts = {
            json.dumps(row, sort_keys=True, default=str)
            for row in empty_runs
        }
        return len(unique_attempts) >= 2

    def _get_tool_schemas(self):
        """Get tool schemas for the model."""
        schemas = []
        allowed_names = self._allowed_skill_tool_names()
        route_names = self._route_tool_names()
        for tool in self.tool_registry.list_tools():
            if route_names is not None and tool["name"] not in route_names:
                continue
            if tool["name"] == router.REQUEST_MORE:
                if route_names is None:
                    continue
            elif self.enforce_skill_tools and tool["name"] not in allowed_names:
                continue
            if not self._is_tool_runtime_enabled(tool["name"]):
                continue
            if tool["name"].startswith("actions.") and (
                self.agent.get("read_only") or not self.agent.get("allow_actions")
            ):
                continue
            schema = {
                "name": tool["name"].replace(".", "__"),
                "description": tool["description"],
                "parameters": tool["parameters"],
            }
            schemas.append(schema)
        return schemas

    def _configure_route(self):
        """Classify the request and narrow the tool set (see runtime/router.py)."""
        self.route = "open_analysis"
        self.extra_tools = []
        if self._routing_enabled():
            self.route = router.classify(
                self.run.prompt,
                has_attachments=self._has_current_attachments(),
                has_document=bool(self.run.get("context_doctype") and self.run.get("context_name")),
            )
            names = router.route_tools(self.route)
            if names:
                self._grant_declared_tools(names)
        try:
            frappe.db.set_value("AI Run", self.run_name, "route", self.route, update_modified=False)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Route Persist Failed: {self.run_name}")

    def _routing_enabled(self):
        settings = frappe.get_cached_doc("AI Settings")
        if settings.get("enable_tool_routing") is not None and not cint(settings.get("enable_tool_routing")):
            return False
        # Specialized agents (JSON contracts, WhatsApp) keep their full tool flow.
        if self._requires_json_final() or self._is_whatsapp_support_agent():
            return False
        return True

    def _route_tool_names(self):
        names = router.route_tools(getattr(self, "route", "open_analysis"))
        if names is None:
            return None
        return set(names) | set(getattr(self, "extra_tools", []))

    def _declared_tool_rows(self, names):
        wanted = set(names)
        return [
            row
            for skill in self.effective_skills
            for row in (skill.get("tools") or [])
            if row.get("tool_name") in wanted
        ]

    def _grant_declared_tools(self, names):
        """Tools a routed question may use without skills.load: only those an
        effective (granted) skill declares, so skill grants still bound access."""
        self.skill_tools = self._merge_skill_tools(self.skill_tools, self._declared_tool_rows(names))
        self.tool_gateway.skill_tools = self.skill_tools

    ALWAYS_REQUESTABLE = {
        "skills.list", "skills.load", "skills.read_reference",
        "schema.search_doctypes", "schema.describe_doctype",
    }

    def _handle_request_more(self, arguments):
        requested = [
            str(name or "").replace("__", ".").strip()
            for name in (arguments.get("tools") or [])
        ][:8]
        declared = {row.get("tool_name") for row in self._declared_tool_rows(requested)}
        added, unavailable = [], []
        for name in requested:
            if not self.tool_registry.get_tool(name) or name == router.REQUEST_MORE:
                unavailable.append(name)
            elif name in declared or name in self.ALWAYS_REQUESTABLE:
                added.append(name)
            else:
                unavailable.append(name)
        if added:
            self.extra_tools = sorted(set(self.extra_tools) | set(added))
            self._grant_declared_tools(added)
        return {"added": added, "unavailable": unavailable}

    def _configure_skill_runtime(self, effective_skills):
        self.effective_skills = list(effective_skills or [])
        declared_tools = [
            tool
            for skill in self.effective_skills
            for tool in (skill.get("tools") or [])
        ]
        self.enforce_skill_tools = bool(declared_tools)
        implicit_tools = [
            tool
            for skill in self.effective_skills
            if skill.get("allow_implicit_invocation")
            for tool in (skill.get("tools") or [])
        ]
        self.skill_tools = self._merge_skill_tools([], implicit_tools)
        self.tool_gateway.skill_tools = self.skill_tools
        self.tool_gateway.enforce_skill_tools = self.enforce_skill_tools

    def _merge_skill_tools(self, current, additions):
        merged = []
        positions = {}
        for row in [*(current or []), *(additions or [])]:
            name = row.get("tool_name") if isinstance(row, dict) else None
            if not name:
                continue
            if name in positions:
                existing = merged[positions[name]]
                existing["maximum_calls"] = max(
                    cint(existing.get("maximum_calls")),
                    cint(row.get("maximum_calls")),
                )
                continue
            positions[name] = len(merged)
            merged.append(dict(row))
        return merged

    def _allowed_skill_tool_names(self):
        names = {row.get("tool_name") for row in self.skill_tools if row.get("tool_name")}
        if any(not skill.get("allow_implicit_invocation") for skill in self.effective_skills):
            names.update({"skills.list", "skills.load", "skills.read_reference"})
        return names

    def _is_tool_runtime_enabled(self, tool_name):
        if not tool_name.startswith("memory."):
            return True
        if self.agent.get("disable_memory"):
            return False
        try:
            return bool(frappe.get_cached_doc("AI Settings").get("enable_memory"))
        except Exception:
            # Deliberately silent: evaluated per tool-schema build, and a failed
            # settings read only hides the optional memory tools.
            return False

    def _check_limits(self):
        settings = frappe.get_cached_doc("AI Settings")
        if not settings.enabled:
            raise MYSYSAgentError("Agent system is disabled.")

    def _check_cancellation(self):
        # Lightweight single-column poll: this runs before every model step and
        # tool call, so a full document reload here is pure waste.
        if frappe.db.get_value("AI Run", self.run_name, "cancel_requested"):
            raise RunCancelled("Run was cancelled by user.")
        started_at = self.run.started_at or self.run.creation
        elapsed = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), started_at)
        if self._timeout_seconds is None:
            self._timeout_seconds = run_timeout_seconds(agent=self.agent)
        if elapsed > self._timeout_seconds:
            raise RunTimedOut("Run exceeded the configured execution time limit.")

    def _add_step(self, step_type, title, summary=""):
        # File-reading progress (and PI page reading) add steps outside this
        # counter; never reuse their sequence numbers.
        self.sequence = max(
            self.sequence + 1,
            cint(frappe.db.count("AI Run Step", {"run": self.run_name})) + 1,
        )
        return self.trace.add_step(
            self.run_name, self.sequence, step_type, title, summary
        )

    def _update_status(self, status, **kwargs):
        self.trace.update_run_status(self.run_name, status, **kwargs)

    def _call_model_step(self, title, *, instructions, input_items, tools, state):
        """Execute one visible model turn so waits and failures are traceable."""
        step = self._add_step("Model", title)
        self.trace.update_run_status(
            self.run_name,
            RUN_STATUS_RUNNING,
            current_step=self.sequence,
            tool_call_count=cint((state or {}).get("tool_call_count")),
        )
        try:
            instructions = dict(instructions or {})
            instructions["messages"] = self._complete_tool_message_protocol(
                instructions.get("messages") or []
            )
            response = self.provider.create_step(
                instructions=instructions,
                input_items=input_items,
                tools=tools,
                state=state,
            )
            self._record_model_usage(response, step)
            if self._is_empty_length_response(response):
                # The model spent the whole token budget (typically on
                # reasoning) and returned nothing. Retry once with a larger
                # budget instead of falling through to the generic fallback.
                retry_instructions = dict(instructions)
                current_max = cint(instructions.get("max_tokens")) or 4096
                retry_instructions["max_tokens"] = max(8192, current_max * 2)
                retry_instructions["reasoning_effort"] = "low"
                retry_instructions["max_retries"] = 1
                response = self.provider.create_step(
                    instructions=retry_instructions,
                    input_items=input_items,
                    tools=tools,
                    state=state,
                )
                self._record_model_usage(response, step)
                length_retry = True
            else:
                length_retry = False
        except Exception as exc:
            self.trace.complete_step(
                step.name,
                status="Failed",
                summary=f"Model request failed: {str(exc)[:350]}",
            )
            raise

        choice = (response or {}).get("choices", [{}])[0]
        message = choice.get("message", {}) or {}
        tool_calls = message.get("tool_calls") or []
        retry_note = " (after a larger-budget retry)" if length_retry else ""
        if tool_calls:
            names = [
                (call.get("function") or {}).get("name", "")
                for call in tool_calls
            ]
            summary = f"Requested {len(tool_calls)} tool call(s): {', '.join(filter(None, names))}{retry_note}"
        elif message.get("content"):
            summary = f"Produced an answer ({len(str(message.get('content')))} characters){retry_note}."
        else:
            summary = f"Returned no answer (finish reason: {choice.get('finish_reason') or 'unknown'}){retry_note}."
        self.trace.complete_step(step.name, summary=summary[:500])
        return response

    def _is_empty_length_response(self, response):
        choice = (response or {}).get("choices", [{}])[0] if isinstance(response, dict) else {}
        message = choice.get("message", {}) or {}
        return (
            str(choice.get("finish_reason") or "").lower() == "length"
            and not message.get("tool_calls")
            and not str(message.get("content") or "").strip()
        )

    def _complete_tool_message_protocol(self, messages):
        """Guarantee one tool response for every assistant tool call before the next turn."""
        normalized = []
        pending_ids = []

        def append_missing():
            for call_id in pending_ids:
                normalized.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps({
                        "error": "Tool execution ended before a response was recorded."
                    }),
                })
            pending_ids.clear()

        for message in messages:
            role = message.get("role") if isinstance(message, dict) else None
            if pending_ids and role != "tool":
                append_missing()

            normalized.append(message)
            if role == "assistant" and message.get("tool_calls"):
                pending_ids.extend(
                    call.get("id")
                    for call in message.get("tool_calls") or []
                    if call.get("id")
                )
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if call_id in pending_ids:
                    pending_ids.remove(call_id)

        if pending_ids:
            append_missing()
        return normalized

    USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

    def _record_model_usage(self, response, step=None):
        if not response:
            return
        try:
            usage = self.provider.get_usage(response) or {}
        except Exception:
            # Best-effort: usage stats are cosmetic and must never fail a run.
            return

        input_tokens = cint(usage.get("input_tokens"))
        output_tokens = cint(usage.get("output_tokens"))
        if not input_tokens and not output_tokens:
            return

        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        cache_read = cint(usage.get("cache_read_input_tokens"))
        cache_write = cint(usage.get("cache_creation_input_tokens"))

        try:
            if step is not None:
                # Per model call; a length retry adds to the same step.
                current = frappe.db.get_value("AI Run Step", step.name, list(self.USAGE_FIELDS), as_dict=True) or {}
                frappe.db.set_value("AI Run Step", step.name, {
                    "input_tokens": cint(current.get("input_tokens")) + input_tokens,
                    "output_tokens": cint(current.get("output_tokens")) + output_tokens,
                    "cache_read_input_tokens": cint(current.get("cache_read_input_tokens")) + cache_read,
                    "cache_creation_input_tokens": cint(current.get("cache_creation_input_tokens")) + cache_write,
                }, update_modified=False)
            self.run.reload()
            self.run.input_tokens = self.input_tokens
            self.run.output_tokens = self.output_tokens
            self.run.cache_read_input_tokens = cint(self.run.get("cache_read_input_tokens")) + cache_read
            self.run.cache_creation_input_tokens = cint(self.run.get("cache_creation_input_tokens")) + cache_write
            self.run.save(ignore_permissions=True)
            frappe.db.commit()
            self.trace.publish_realtime(REALTIME_EVENTS["run_updated"], self.run_name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Usage Persist Failed: {self.run_name}")

    def _complete_run(self, final_answer):
        if not final_answer:
            raise MYSYSAgentError("The model did not produce a final answer.")

        final_answer = self._ensure_required_json_final(final_answer)
        final_answer = self._sanitize_final_answer(final_answer)
        failure_reason = answer_contract.structured_failure_reason(self._current_skill_policy(), final_answer)
        final_status = RUN_STATUS_FAILED if failure_reason else RUN_STATUS_COMPLETED
        self.run.reload()
        self.run.final_answer = final_answer
        self.run.completed_at = frappe.utils.now_datetime()
        self.run.status = final_status
        self.run.error_summary = failure_reason[:500] if failure_reason else None
        self.run.current_step = self.sequence
        self.run.tool_call_count = self.run.tool_call_count or 0
        self.run.input_tokens = self.input_tokens
        self.run.output_tokens = self.output_tokens
        self.run.save(ignore_permissions=True)
        if not failure_reason:
            self._store_lesson_from_completed_run(final_answer)
        frappe.db.commit()
        self._update_compact_session_checkpoint(
            final_status,
            failure_reason or "Final answer saved successfully.",
        )
        event = "run_failed" if failure_reason else "run_completed"
        self.trace.publish_realtime(REALTIME_EVENTS[event], self.run_name)

    def _ensure_required_json_final(self, final_answer):
        if not self._requires_json_final():
            return final_answer
        scalar_keys = {"status", "message", "summary", "client_readable_summary", "reply_markdown"}
        if answer_contract.looks_like_tool_markup(final_answer):
            # Never mine JSON out of tool-call markup: the first parseable
            # object would be a tool argument, not the answer.
            final_answer = answer_contract.strip_tool_markup(final_answer)
        parsed = answer_contract.extract_json_object(final_answer)
        if isinstance(parsed, dict):
            missing_keys = []
            for key in self._current_skill_policy().get("required_output_keys") or []:
                if key not in parsed:
                    parsed[key] = "" if key in scalar_keys else []
                    missing_keys.append(key)
            if missing_keys:
                normalize_step = self._add_step(
                    "Final",
                    "Normalizing final JSON contract",
                    summary=f"Added missing empty fields: {', '.join(missing_keys)}",
                )
                self.trace.complete_step(normalize_step.name)
            return json.dumps(parsed, ensure_ascii=False, default=str)

        fallback_step = self._add_step(
            "Final",
            "Rejecting invalid final JSON",
            summary="The model did not return a JSON object; no hidden model retry was performed.",
        )
        self.trace.complete_step(fallback_step.name, status="Failed")
        return json.dumps(
            answer_contract.fallback_json_final_answer(self._current_skill_policy(), final_answer),
            ensure_ascii=False,
            default=str,
        )

    def _requires_json_final(self):
        # Only the skill policy may demand a JSON contract. Sniffing markers
        # out of the prompt let user-typed text flip the enforcement pipeline
        # and mangle valid Markdown answers.
        return self._current_skill_policy().get("response_format") == "JSON Object"

    def _current_skill_policy(self):
        policies = [
            skill.get("runtime_policy") or {}
            for skill in self.effective_skills
            if skill.get("allow_implicit_invocation")
        ]
        policies.extend(skill.get("runtime_policy") or {} for skill in self.loaded_skills)
        return answer_contract.merge_skill_policies(policies)

    def _sanitize_final_answer(self, final_answer):
        text = str(final_answer or "")
        # Any answer that IS a JSON object (required or voluntary interactive
        # JSON) must pass through untouched: the line filter and the 16k
        # truncation below would corrupt it.
        if answer_contract.extract_json_object(text) is not None and text.strip().startswith(("{", "```")):
            return text.strip()
        text = answer_contract.strip_tool_markup(text)
        if len(text) > 16000:
            text = (
                text[:16000].rstrip()
                + "\n\nتم اختصار الرد الطويل. إذا كانت هناك بيانات كبيرة فهي محفوظة في الجدول المرفق أسفل الرد للعرض والطباعة والتصدير."
            )
        return text or "تم تنفيذ الخطوة، ويمكنك المتابعة من الزر المقترح إذا كان الطلب يحتاج خطوات إضافية."

    def _fail_run(self, error_summary):
        try:
            self.run.reload()
            self.run.error_summary = error_summary[:500]
            self.run.completed_at = frappe.utils.now_datetime()
            self.run.status = RUN_STATUS_FAILED
            self.run.input_tokens = self.input_tokens
            self.run.output_tokens = self.output_tokens
            self.run.save(ignore_permissions=True)
            self._store_lesson_from_failed_run(error_summary)
            frappe.db.commit()
            self._update_compact_session_checkpoint(
                RUN_STATUS_FAILED,
                error_summary,
            )
            self.trace.publish_realtime(REALTIME_EVENTS["run_failed"], self.run_name)
        except Exception:
            # A run we cannot mark Failed will look stuck until the orphan
            # watchdog recovers it — that must never happen silently.
            frappe.log_error(frappe.get_traceback(), f"AI Agent Fail-Run Persist Failed: {self.run_name}")

    def _time_out_run(self, error_summary):
        try:
            self.run.reload()
            self.run.status = RUN_STATUS_TIMED_OUT
            self.run.error_summary = error_summary
            self.run.completed_at = frappe.utils.now_datetime()
            self.run.current_step = self.sequence
            self.run.input_tokens = self.input_tokens
            self.run.output_tokens = self.output_tokens
            self.run.save(ignore_permissions=True)
            frappe.db.commit()
            self._update_compact_session_checkpoint(RUN_STATUS_TIMED_OUT, error_summary)
            self.trace.publish_realtime(REALTIME_EVENTS["run_failed"], self.run_name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"AI Agent Timeout Persist Failed: {self.run_name}")

    def _update_compact_session_checkpoint(self, status, summary="", state=None):
        """Persist a bounded latest-run checkpoint without replacing agent instructions."""
        try:
            latest_run = frappe.get_all(
                "AI Run",
                filters={"agent": self.run.agent},
                fields=["name"],
                order_by="creation desc",
                page_length=1,
            )
            if latest_run and latest_run[0].name != self.run_name:
                return

            current = frappe.db.get_value("AI Agent", self.agent.name, "compact_session") or ""
            base = str(current).split(self.COMPACT_CHECKPOINT_START, 1)[0].rstrip()
            recent_steps = frappe.get_all(
                "AI Run Step",
                filters={"run": self.run_name},
                fields=["sequence", "step_type", "title", "status", "summary"],
                order_by="sequence desc",
                page_length=5,
            )
            recent_steps.reverse()
            step_lines = []
            for row in recent_steps:
                detail = " ".join(str(row.summary or "").split())[:280]
                step_lines.append(
                    f"- {row.sequence}: {row.step_type}/{row.status} - {row.title}"
                    + (f" | {detail}" if detail else "")
                )
            tool_count = state.get("tool_call_count", 0) if isinstance(state, dict) else cint(
                frappe.db.get_value("AI Run", self.run_name, "tool_call_count")
            )
            current_sequence = max(
                self.sequence,
                cint(frappe.db.get_value("AI Run", self.run_name, "current_step")),
                max((cint(row.sequence) for row in recent_steps), default=0),
            )
            checkpoint = "\n".join([
                self.COMPACT_CHECKPOINT_START,
                "Latest runtime checkpoint (informational; run evidence remains authoritative):",
                f"Run: {self.run_name}",
                f"Status: {status}",
                f"Objective: {' '.join(str(self.run.prompt or '').split())[:700]}",
                f"Current sequence: {current_sequence}",
                f"Tool calls: {tool_count}",
                f"Latest summary: {' '.join(str(summary or '').split())[:700]}",
                "Recent steps:",
                *(step_lines or ["- No persisted steps yet."]),
                self.COMPACT_CHECKPOINT_END,
            ])
            value = f"{base}\n\n{checkpoint}" if base else checkpoint
            frappe.db.set_value("AI Agent", self.agent.name, "compact_session", value, update_modified=False)
            frappe.db.commit()
            self.agent.compact_session = value
        except Exception:
            frappe.log_error(frappe.get_traceback(), "AI Agent Compact Session Checkpoint")

    def _store_lesson_from_completed_run(self, final_answer):
        if self.agent.get("disable_memory"):
            return
        self.memory.store_completed_run(self.run, final_answer)

    def _store_lesson_from_failed_run(self, error_summary):
        if self.agent.get("disable_memory"):
            return
        lesson = (
            f"Previous run failed. User prompt: {self.run.prompt}. "
            f"Error: {error_summary}"
        )
        self.memory.store_run_lesson(self.run, lesson)


def execute_run(run_name):
    """Entry point for background job.

    Tools read data through the session (frappe.get_list, has_permission), so
    the session must be the run owner: otherwise a job queued by another user
    (or the scheduler's Administrator) would read with the wrong permissions.
    """
    run_user = frappe.db.get_value("AI Run", run_name, "user")
    if not run_user or run_user == "Guest" or frappe.session.user != run_user:
        from ai_agent.runtime.run_control import mark_interrupted_run

        frappe.log_error(
            f"AI Run {run_name} refused: session user {frappe.session.user!r} is not the run owner {run_user!r}.",
            "AI Agent Run Rejected",
        )
        mark_interrupted_run(run_name, frappe._("Agent execution was rejected: the run must execute as its owner."))
        return
    orchestrator = Orchestrator(run_name)
    orchestrator.execute()
