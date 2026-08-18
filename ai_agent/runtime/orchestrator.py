import json
import hashlib
import re
import frappe
from frappe import _
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
from ai_agent.runtime.run_control import run_timeout_seconds
from ai_agent.runtime.exceptions import (
    RunCancelled, RunTimedOut, RunLimitExceeded, MYSYSAgentError,
    ToolDenied, PermissionDenied,
)
from ai_agent.runtime.constants import (
    RUN_STATUS_QUEUED, RUN_STATUS_PLANNING, RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING, RUN_STATUS_COMPLETED, RUN_STATUS_FAILED,
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
        self.provider = get_provider()
        self.tool_gateway = ToolGateway(self.ctx, self.tool_registry)
        self.sequence = 0
        self.loaded_skills = []
        self.skill_tools = []
        self.effective_skills = []
        self.enforce_skill_tools = False
        self.input_tokens = cint(self.run.input_tokens)
        self.output_tokens = cint(self.run.output_tokens)
        self.execution_state = None

    def execute(self):
        """Main execution entry point."""
        try:
            self._update_status(RUN_STATUS_PLANNING)
            self._check_limits()

            effective_skills = self.skill_registry.list_effective_skills(
                self.run.user, self.run.agent, self.run.company
            )
            self._configure_skill_runtime(effective_skills)

            plan_step = self._add_step("Plan", "Analyzing task and building plan")

            skill_metadata = self._select_context_skills(effective_skills)

            tool_schemas = self._get_tool_schemas()

            context = self.context_builder.build(
                self.run, self.agent, skill_metadata, tool_schemas
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
            if not self.run.parent_run and not self._has_current_attachments():
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
            self.execution_state = state

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
        except Exception as e:
            frappe.log_error(f"AI Agent error: {str(e)}", "AI Agent")
            self._fail_run("An unexpected error occurred.")

    def _run_loop(self, state):
        """Main model loop."""
        max_steps = self.agent.maximum_steps or 12
        max_tool_calls = self.agent.maximum_tool_calls or 25

        while state["step_count"] < max_steps:
            self._check_cancellation()

            state["step_count"] += 1

            policy = self._current_skill_policy()
            response = self._call_model_step(
                "Analyzing context and selecting the next action",
                instructions={
                    "messages": state["messages"],
                    "model": state["model"],
                    "reasoning_effort": state.get("reasoning_effort"),
                    "max_tokens": policy.get("finalization_max_tokens") or None,
                    "response_format": (
                        {"type": "json_object"}
                        if policy.get("response_format") == "JSON Object"
                        else None
                    ),
                    "max_retries": 1,
                },
                input_items=[],
                tools=self._get_tool_schemas(),
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

                    step = self._add_step("Tool", self._tool_step_title(tool_name, arguments))
                    self.trace.update_run_status(
                        self.run_name,
                        RUN_STATUS_RUNNING,
                        tool_call_count=state["tool_call_count"],
                        current_step=self.sequence,
                    )

                    try:
                        if tool_name.startswith("actions.") and "request" in tool_name:
                            result = self._handle_action_tool(tool_name, arguments)
                        else:
                            result = self.tool_gateway.authorize_and_execute(
                                tool_name, arguments, self.run_name, step.name, self.agent
                            )
                    except (ToolDenied, PermissionDenied) as exc:
                        result = {"error": str(exc)}
                    self._track_report_result(state, tool_name, arguments, result)
                    result = self._maybe_create_approval_from_prepare(tool_name, result)
                    if tool_name == "skills.load" and isinstance(result, dict) and "error" not in result:
                        self.loaded_skills.append(result)
                        self.skill_tools = self._merge_skill_tools(
                            self.skill_tools,
                            result.get("tools") or [],
                        )
                        self.tool_gateway.skill_tools = self.skill_tools
                    tool_summary = self._summarize_tool_result(tool_name, result, arguments)
                    self.trace.complete_step(
                        step.name,
                        summary=tool_summary,
                    )
                    self._update_compact_session_checkpoint(
                        RUN_STATUS_RUNNING,
                        tool_summary,
                        state=state,
                    )

                    model_result = self._compact_tool_result_for_model(tool_name, result)
                    self._record_tool_observation(state, tool_name, arguments, model_result)

                    # Every assistant tool_call_id must receive a tool response before
                    # another model request, even when execution should stop afterward.
                    state["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": self._tool_result_content_for_model(tool_name, model_result),
                    })

                    if state["tool_attempts"][attempt_key] >= 2:
                        stop_reason = (
                            f"Stopped because the same tool call repeated "
                            f"{state['tool_attempts'][attempt_key]} times."
                        )

                    if self._is_empty_report_loop(state):
                        empty_report_answer = self._empty_report_loop_answer(state)

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

            elif message.get("content"):
                final_step = self._add_step("Final", "Generating final answer", summary=message["content"][:500])
                self.trace.complete_step(final_step.name)
                return message["content"]
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
            final_answer = self._answer_from_successful_report(state["last_successful_report"])
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
        }
        ordered = sorted(
            effective_skills or [],
            key=lambda skill: (priority.get(skill.get("name"), 50), skill.get("name") or ""),
        )
        return ordered[:12]

    def _try_local_fast_answer(self):
        if self._is_whatsapp_support_agent():
            return None
        if self._has_current_attachments():
            return None
        prompt = self._normalize_prompt(self.run.prompt)
        if not prompt:
            return None
        action_markers = (
            "انش", "اضف", "سوي", "اعمل", "حدث", "عدل", "احذف", "الغ",
            "فاتور", "سند", "مورد", "عميل", "صنف", "تقرير", "كشف", "رصيد",
            "create", "add", "update", "delete", "invoice", "report",
        )
        if any(marker in prompt for marker in action_markers):
            return None
        capability_markers = (
            "هل تستطيع", "تقدر", "هل تقدر", "قدراتك", "ما تستطيع",
            "المهام الكبيره", "المهام المركبه", "شغل كبير", "can you",
        )
        if any(marker in prompt for marker in capability_markers):
            return (
                "نعم، أقدر أتعامل مع المهام الكبيرة والمركبة داخل ERP.\n\n"
                "- أجهز مستندات كبيرة مثل فواتير فيها أصناف كثيرة.\n"
                "- أبحث عن الأصناف/الموردين/العملاء الموجودين قبل إنشاء جديد.\n"
                "- أجهز المتطلبات الناقصة أولًا ثم أتابع المستند الأساسي.\n"
                "- كل عملية حفظ أو تعديل تتم عبر موافقة واضحة قبل التنفيذ."
            )
        greeting_markers = ("هلا", "اهلا", "مرحبا", "السلام", "من انت", "مين انت", "who are you", "hello", "hi")
        if not any(marker in prompt for marker in greeting_markers):
            return None
        return (
            "أهلًا 👋 أنا **الباشق**.\n\n"
            "أقدر أساعدك في التحليل، التقارير، قراءة الملفات، وتجهيز المستندات مثل الفواتير والأصناف والموردين مع طلب موافقة قبل أي حفظ."
        )

    def _normalize_prompt(self, value):
        return (
            str(value or "")
            .lower()
            .replace("أ", "ا")
            .replace("إ", "ا")
            .replace("آ", "ا")
            .replace("ة", "ه")
            .replace("ى", "ي")
            .strip()
        )

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
            return None

        route = self._parse_json_message_response(response)
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
            return False
        return any(skill.get("name") == "whatsapp-business-support" for skill in skills or [])

    def _has_current_attachments(self):
        try:
            return bool(FileContextService().get_run_files(self.run_name))
        except Exception:
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
        route = self._parse_json_text(content)
        if route.get("route") == "continue":
            return None
        if route.get("answer") and not requires_json:
            return route.get("answer")
        return content.strip() or None

    def _parse_json_message_response(self, response):
        choice = response.get("choices", [{}])[0] if isinstance(response, dict) else {}
        content = (choice.get("message", {}) or {}).get("content") or ""
        return self._parse_json_text(content)

    def _parse_json_text(self, content):
        text = (content or "").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except Exception:
                return {}
        return {}

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
        if result.get("terminal") is True or result.get("exhaustive") is True:
            return True
        if result.get("is_pending") or result.get("prepared_status") in ("Queued", "Started", "Missing"):
            return True
        if tool_name == "reports.run" and "row_count" in result:
            return cint(result.get("row_count")) > 0
        if tool_name == "actions.validate_document" and result.get("valid") is True:
            return True
        if tool_name.startswith("actions.") and result.get("approval_id"):
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
                    self._fallback_json_final_answer(reason),
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
            "summary": self._summarize_tool_result(tool_name, result, arguments),
        })
        if len(observations) > 12:
            del observations[:-12]

    def _track_report_result(self, state, tool_name, arguments, result):
        if tool_name != "reports.run" or not isinstance(result, dict) or result.get("error"):
            return

        if cint(result.get("row_count")) > 0:
            state["successful_report_runs"] = state.get("successful_report_runs", 0) + 1
            state["last_successful_report"] = self._compact_tool_result_for_model(tool_name, result)
            return

        state.setdefault("empty_report_runs", []).append({
            "report": result.get("report") or arguments.get("report_name"),
            "filters": result.get("filters") or arguments.get("filters") or {},
        })

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

    def _empty_report_loop_answer(self, state):
        attempts = state.get("empty_report_runs") or []
        reports = []
        for attempt in attempts:
            report = attempt.get("report")
            if report and report not in reports:
                reports.append(report)
        filters = attempts[-1].get("filters") if attempts else {}
        return (
            "لم أُكمل التحليل لأن التقارير المرشحة رجعت بدون صفوف، "
            "وأوقفت التنفيذ حتى لا يستهلك خطوات وتوكنز بتكرار نفس المحاولات.\n\n"
            f"- التقارير التي جُربت: {', '.join(reports[:5]) or 'غير محدد'}\n"
            f"- آخر فلاتر مستخدمة: `{json.dumps(filters, ensure_ascii=False, default=str)}`\n"
            "- السبب المرجح: التقرير يحتاج فلاتر مختلفة عن الأسماء التي تم تمريرها، "
            "أو لا توجد بيانات في هذا التقرير للفترة المطلوبة.\n"
            "- الخطوة الأفضل: استخدم استعلام تجميعي مباشر على بيانات القيود/الفواتير أو اختر تقريراً محدداً بفلاتره الصحيحة."
        )

    def _answer_from_successful_report(self, report_result):
        report_name = report_result.get("report") or "التقرير"
        row_count = cint(report_result.get("row_count"))
        summary = report_result.get("summary") or {}
        numeric_stats = summary.get("numeric_stats") or {}
        top_rows = summary.get("top_rows_by_numeric_field") or {}
        filters = report_result.get("filters") or {}

        lines = [
            f"وجدت بيانات في تقرير **{report_name}** لكن أوقفت التنفيذ قبل تكرار خطوات إضافية.",
            "",
            f"- عدد الصفوف: **{row_count}**",
        ]

        if filters:
            lines.append(f"- الفلاتر: `{json.dumps(filters, ensure_ascii=False, default=str)}`")

        for field, stats in list(numeric_stats.items())[:5]:
            lines.append(f"- إجمالي `{field}`: **{stats.get('total')}**")

        for field, rows in list(top_rows.items())[:2]:
            if not rows:
                continue
            lines.append("")
            lines.append(f"أعلى القيم حسب `{field}`:")
            for index, row in enumerate(rows[:5], start=1):
                label = row.get("label") or "بدون تسمية"
                lines.append(f"{index}. {label}: **{row.get('value')}**")

        lines.append("")
        lines.append("هذه إجابة مختصرة من نتيجة التقرير الناجحة لتجنب الوصول إلى حد الخطوات.")
        return "\n".join(lines)

    def _answer_from_tool_observations(self, state, reason):
        observations = state.get("tool_observations") or []
        if not observations:
            return "تم استلام طلبك، ولم أتمكن من تجهيز النتيجة النهائية الآن. سيتم متابعة الطلب والرد عليك بالتفاصيل المناسبة."

        useful_observations = self._select_useful_observations(observations)
        first = useful_observations[0] if useful_observations else {}
        result = first.get("result") if isinstance(first, dict) else None
        if isinstance(result, dict) and cint(result.get("row_count")) > 0:
            rows = result.get("sample_rows") or result.get("rows") or []
            summary = [f"تم العثور على {cint(result.get('row_count'))} سجل مطابق."]
            if rows:
                summary.append(f"أبرز نتيجة: {self._compact_summary(rows[0], limit=500)}")
            return " ".join(summary)

        lines = [
            "وجدت نتائج مرتبطة بطلبك، لكن لم أتمكن من صياغة الرد النهائي بالكامل الآن.",
        ]

        for observation in useful_observations[:2]:
            lines.extend(self._format_observation(observation))

        return "\n".join(lines)

    def _select_useful_observations(self, observations):
        scored = []
        for index, observation in enumerate(observations):
            result = observation.get("result")
            score = 0
            if isinstance(result, dict):
                if result.get("error"):
                    score -= 2
                if cint(result.get("row_count")) > 0:
                    score += 4
                if result.get("approval_required"):
                    score += 4
                if result.get("valid") is True:
                    score += 3
                if result.get("count"):
                    score += 2
                if result.get("summary"):
                    score += 2
                if result.get("sample_rows") or result.get("rows"):
                    score += 3
            scored.append((score, index, observation))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [row[2] for row in scored]

    def _format_observation(self, observation):
        tool_name = observation.get("tool")
        result = observation.get("result")
        lines = ["", f"**{tool_name}**"]

        if not isinstance(result, dict):
            lines.append(f"- النتيجة: `{self._compact_summary(result, limit=500)}`")
            return lines

        if result.get("error"):
            lines.append(f"- تعذر التنفيذ: {result.get('error')}")
            return lines

        if result.get("is_pending") or result.get("prepared_status") in ("Queued", "Started"):
            lines.append("- التقرير يحتاج تجهيز مسبق.")
            lines.append(f"- الحالة: **{result.get('prepared_status') or 'Pending'}**")
            if result.get("prepared_report_name"):
                lines.append(f"- Prepared Report: `{result.get('prepared_report_name')}`")
            if result.get("created_prepared_report"):
                lines.append("- تم إنشاء طلب توليد جديد.")
            if result.get("message"):
                lines.append(f"- ملاحظة: {result.get('message')}")
            return lines

        if "row_count" in result:
            lines.append(f"- عدد الصفوف: **{result.get('row_count')}**")
            rows = result.get("sample_rows") or result.get("rows") or []
            for index, row in enumerate(rows[:3], start=1):
                lines.append(f"- عينة {index}: `{self._compact_summary(row, limit=350)}`")
            return lines

        if result.get("approval_required"):
            lines.append(f"- يحتاج موافقة: `{result.get('approval_id')}`")
            if result.get("approval_status"):
                lines.append(f"- حالة الموافقة: **{result.get('approval_status')}**")
            return lines

        if result.get("valid") is not None:
            lines.append(f"- صالح: **{result.get('valid')}**")
            if result.get("errors"):
                lines.append(f"- الأخطاء: {result.get('errors')}")
            return lines

        if result.get("count") is not None:
            lines.append(f"- العدد: **{result.get('count')}**")
            return lines

        lines.append(f"- النتيجة: `{self._compact_summary(result, limit=500)}`")
        return lines

    def _tool_step_title(self, tool_name, arguments):
        if tool_name == "data.query":
            spec = self._query_spec(arguments)
            doctype = spec.get("doctype") or "السجلات"
            return f"البحث في {doctype}"

        if tool_name == "data.count":
            return f"عدّ سجلات {arguments.get('doctype') or 'النظام'}"

        if tool_name == "reports.run":
            report_name = arguments.get("report_name") or arguments.get("name") or arguments.get("report") or ""
            return f"تشغيل تقرير {report_name}".strip()

        if tool_name == "reports.search":
            return "البحث عن التقارير"

        if tool_name == "reports.describe":
            return "قراءة إعداد التقرير"

        if tool_name == "schema.search_doctypes":
            return "استكشاف أنواع المستندات"

        if tool_name == "schema.describe_doctype":
            return "قراءة بنية نوع المستند"

        if tool_name == "skills.list":
            return "فحص المهارات المتاحة"

        if tool_name == "skills.load":
            return f"تحميل مهارة {arguments.get('skill_name') or ''}".strip()

        if tool_name.startswith("actions.request_"):
            return "طلب موافقة"

        if tool_name.startswith("actions.prepare_"):
            return "تجهيز مستند"

        if tool_name.startswith("actions.validate_"):
            return "التحقق من مستند"

        return f"تشغيل {tool_name}"

    def _summarize_tool_result(self, tool_name, result, arguments=None):
        arguments = arguments or {}
        if not isinstance(result, dict):
            return self._compact_summary(result)

        if result.get("error"):
            return f"تعذر تنفيذ {self._tool_action_phrase(tool_name, arguments)}: {result.get('error')}"

        if tool_name == "data.query":
            return self._summarize_data_query(arguments, result)

        if tool_name == "data.count":
            return self._summarize_data_count(arguments, result)

        if tool_name == "reports.run":
            report_name = arguments.get("report_name") or arguments.get("name") or arguments.get("report") or result.get("report", "Report")
            filters_text = self._human_filters(arguments.get("filters") or result.get("filters") or {})
            if result.get("is_pending") or result.get("prepared_status") in ("Queued", "Started"):
                return (
                    f"شغلت التقرير {report_name}"
                    f"{filters_text}. التقرير يحتاج تجهيز مسبق؛ الحالة "
                    f"{result.get('prepared_status') or 'Pending'}، "
                    f"Prepared Report={result.get('prepared_report_name') or 'unknown'}"
                )
            summary = result.get("summary") or {}
            numeric_fields = summary.get("numeric_fields") or []
            return (
                f"شغلت التقرير {report_name}{filters_text}. "
                f"رجع {result.get('row_count', 0)} صفوف، "
                f"والحقول الرقمية المتاحة: {', '.join(numeric_fields[:8]) or 'لا يوجد'}"
            )

        if tool_name.startswith("actions."):
            payload = result.get("payload") or {}
            payload_summary = self._summarize_payload(payload) if payload else {}
            parts = []
            if result.get("doctype"):
                parts.append(f"المستند={result.get('doctype')}")
            if result.get("approval_required"):
                parts.append(f"بطاقة الموافقة={result.get('approval_id')}")
            if result.get("missing_fields"):
                parts.append(f"حقول ناقصة={len(result.get('missing_fields') or [])}")
            if payload_summary:
                parts.append(
                    f"حقول={payload_summary.get('field_count', 0)} "
                    f"صفوف فرعية={payload_summary.get('total_child_rows', 0)}"
                )
            return "; ".join(parts) or self._compact_summary(result)

        if "doctypes" in result:
            names = [row.get("name") for row in result.get("doctypes", []) if row.get("name")]
            return f"وجدت {result.get('count', len(names))} DocTypes: {', '.join(names[:10]) or 'لا يوجد'}"

        if "fields" in result:
            return (
                f"{result.get('name', tool_name)}: "
                f"{len(result.get('fields', []))} حقول، "
                f"{len(result.get('links', []))} روابط، "
                f"{len(result.get('child_tables', []))} جداول فرعية."
            )

        if "rows" in result:
            rows = result.get("rows") or []
            if not rows:
                return f"{self._tool_action_phrase(tool_name, arguments)} ولم ترجع أي صفوف."
            return (
                f"{self._tool_action_phrase(tool_name, arguments)} ورجعت {result.get('row_count', len(rows))} صفوف. "
                f"أول نتيجة: {self._compact_summary(rows[0], limit=700)}"
            )

        if "count" in result:
            return f"{self._tool_action_phrase(tool_name, arguments)}. العدد: {result.get('count')}"

        if result.get("approval_required"):
            return f"تحتاج موافقة: {result.get('approval_id')}"

        return self._compact_summary(result)

    def _summarize_data_query(self, arguments, result):
        spec = self._query_spec(arguments)
        doctype = spec.get("doctype") or "records"
        fields = self._human_fields(spec.get("fields") or [])
        filters = self._human_filters(spec.get("filters") or [])
        order = f"، الترتيب: {spec.get('order_by')}" if spec.get("order_by") else ""
        limit = f"، الحد: {spec.get('limit')}" if spec.get("limit") else ""
        rows = result.get("rows") or []
        prefix = f"بحثت في {doctype} عن {fields}{filters}{order}{limit}"
        if not rows:
            return f"{prefix}. النتيجة: لا توجد سجلات مطابقة."
        return (
            f"{prefix}. النتيجة: {result.get('row_count', len(rows))} سجلات. "
            f"أول سجل: {self._compact_summary(rows[0], limit=700)}"
        )

    def _summarize_data_count(self, arguments, result):
        doctype = arguments.get("doctype") or "records"
        filters = self._human_filters(arguments.get("filters") or [])
        return f"عديت سجلات {doctype}{filters}. النتيجة: {result.get('count', 0)}."

    def _tool_action_phrase(self, tool_name, arguments):
        if tool_name == "data.query":
            spec = self._query_spec(arguments)
            return f"بحثت في {spec.get('doctype') or 'السجلات'}"
        if tool_name == "data.count":
            return f"عديت سجلات {arguments.get('doctype') or 'النظام'}"
        if tool_name == "reports.run":
            return f"شغلت التقرير {arguments.get('report_name') or arguments.get('name') or arguments.get('report') or ''}".strip()
        if tool_name == "skills.load":
            return f"تحميل المهارة {arguments.get('skill_name') or ''}".strip()
        return f"تنفيذ {tool_name}"

    def _query_spec(self, arguments):
        spec = arguments.get("specification") if isinstance(arguments, dict) else None
        if isinstance(spec, dict):
            return spec
        return arguments if isinstance(arguments, dict) else {}

    def _human_fields(self, fields):
        if not fields:
            return "الحقول الأساسية"
        labels = []
        for field in fields[:8]:
            if isinstance(field, dict):
                name = field.get("alias") or field.get("field") or field.get("aggregate")
                aggregate = field.get("aggregate")
                labels.append(f"{aggregate}({field.get('field')}) as {name}" if aggregate else str(name))
            else:
                labels.append(str(field))
        suffix = "..." if len(fields) > 8 else ""
        return ", ".join(labels) + suffix

    def _human_filters(self, filters):
        if not filters:
            return ""
        if isinstance(filters, dict):
            items = [f"{key}={value}" for key, value in list(filters.items())[:8]]
            suffix = "..." if len(filters) > 8 else ""
            return f" مع الفلاتر: {', '.join(items)}{suffix}"
        if isinstance(filters, list):
            items = []
            for flt in filters[:8]:
                if isinstance(flt, dict):
                    items.append(
                        f"{flt.get('field')} {flt.get('operator') or '='} {flt.get('value')}"
                    )
                elif isinstance(flt, (list, tuple)) and len(flt) >= 3:
                    items.append(f"{flt[0]} {flt[1]} {flt[2]}")
                else:
                    items.append(str(flt))
            suffix = "..." if len(filters) > 8 else ""
            return f" مع الفلاتر: {', '.join(items)}{suffix}"
        return f" مع الفلاتر: {self._compact_summary(filters, limit=300)}"

    def _compact_summary(self, value, limit=900):
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) > limit:
            return text[:limit].rstrip() + "..."
        return text

    def _compact_tool_result_for_model(self, tool_name, result):
        if not isinstance(result, dict):
            return result

        if tool_name == "reports.run":
            return {
                "report": result.get("report"),
                "filters": result.get("filters"),
                "row_count": result.get("row_count"),
                "summary": result.get("summary"),
                "sample_rows": (result.get("result") or [])[:20],
                "is_truncated": result.get("is_truncated"),
                "payload_mode": result.get("payload_mode"),
                "prepared_report": result.get("prepared_report"),
                "prepared_report_name": result.get("prepared_report_name"),
                "prepared_status": result.get("prepared_status"),
                "is_pending": result.get("is_pending"),
                "created_prepared_report": result.get("created_prepared_report"),
                "message": result.get("message"),
                "model_note": "Use summary first; request raw rows only if the user explicitly asks for row-level detail.",
            }

        if tool_name == "data.query" and "rows" in result:
            rows = result.get("rows") or []
            return {
                "row_count": result.get("row_count", len(rows)),
                "rows": rows[:50],
                "truncated": result.get("truncated") or len(rows) > 50,
                "model_note": "Rows are capped for context efficiency; use aggregation or narrower filters for more detail.",
            }

        if tool_name.startswith("actions."):
            compact = {
                key: result.get(key)
                for key in (
                    "doctype", "valid", "errors", "missing_fields", "approval_required",
                    "approval_id", "approval_status", "action_summary",
                )
                if key in result
            }
            payload = result.get("payload")
            if isinstance(payload, dict):
                compact["payload_summary"] = self._summarize_payload(payload)
                compact["payload_hash"] = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, default=str).encode()
                ).hexdigest()
                compact["payload_omitted_for_context"] = True
            return compact or result

        return result

    def _tool_result_content_for_model(self, tool_name, result):
        """Apply the registered tool's model-context limit."""
        content = json.dumps(result, default=str, ensure_ascii=False)
        tool = self.tool_registry.get_tool(tool_name) or {}
        limit = cint(tool.get("model_result_max_chars")) or 4000
        return content[:limit]

    def _summarize_payload(self, payload):
        if not isinstance(payload, dict):
            return {"type": type(payload).__name__}

        scalar_fields = []
        child_tables = []
        total_child_rows = 0
        nested_objects = []

        for field, value in payload.items():
            if isinstance(value, list):
                row_count = len(value)
                total_child_rows += row_count
                sample_keys = []
                for row in value[:1]:
                    if isinstance(row, dict):
                        sample_keys = list(row.keys())[:20]
                child_tables.append({
                    "field": field,
                    "rows": row_count,
                    "sample_fields": sample_keys,
                })
            elif isinstance(value, dict):
                nested_objects.append({"field": field, "fields": list(value.keys())[:20]})
            else:
                scalar_fields.append(field)

        payload_json = json.dumps(payload, sort_keys=True, default=str)
        return {
            "field_count": len(payload),
            "scalar_fields": scalar_fields[:40],
            "child_tables": child_tables,
            "nested_objects": nested_objects,
            "total_child_rows": total_child_rows,
            "estimated_bytes": len(payload_json.encode()),
            "is_large": len(payload_json) > 4000 or total_child_rows > 50,
        }

    def _handle_action_tool(self, tool_name, arguments):
        """Handle action tools through the approval engine."""
        self.tool_gateway._validate_arguments(tool_name, arguments)
        self._update_status(RUN_STATUS_WAITING)

        if tool_name == "actions.request_create_draft":
            payload = self._document_payload(arguments)
            approval = self._create_or_get_create_approval(arguments.get("doctype"), payload)
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_bulk_create":
            documents = self._bulk_create_documents(arguments.get("documents") or [])
            if not documents:
                return {"error": "documents is required"}
            denied = [
                document["doctype"]
                for document in documents
                if not self.ctx.has_doctype_permission(document["doctype"], "create")
            ]
            if denied:
                return {"error": f"No create permission on: {', '.join(sorted(set(denied)))}"}
            approval = self._create_or_get_bulk_create_approval(
                documents,
                arguments.get("summary"),
            )
            return {
                "approval_required": True,
                "approval_id": approval.name,
                "document_count": len(documents),
                "doctypes": sorted({document["doctype"] for document in documents}),
            }

        elif tool_name == "actions.request_update":
            payload = self._document_payload(arguments, include_doctype=False)
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Update",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=f"Update {arguments.get('doctype')} {arguments.get('document_name')}",
                payload=payload,
                expected_modified=arguments.get("expected_modified"),
            )
            self._record_approval_step(
                f"Approval required: Update {arguments.get('doctype')}",
                approval.name,
            )
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_submit":
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Submit",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=f"Submit {arguments.get('doctype')} {arguments.get('document_name')}",
                impact_summary="The document will be submitted.",
                payload={"doctype": arguments.get("doctype"), "name": arguments.get("document_name")},
            )
            self._record_approval_step(
                f"Approval required: Submit {arguments.get('doctype')}",
                approval.name,
            )
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_cancel":
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Cancel",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=f"Cancel {arguments.get('doctype')} {arguments.get('document_name')}",
                impact_summary="The document will be cancelled.",
                payload={"doctype": arguments.get("doctype"), "name": arguments.get("document_name")},
            )
            self._record_approval_step(
                f"Approval required: Cancel {arguments.get('doctype')}",
                approval.name,
            )
            return {"approval_required": True, "approval_id": approval.name}

        return {"error": f"Unknown action tool: {tool_name}"}

    def _maybe_create_approval_from_prepare(self, tool_name, result):
        if tool_name != "actions.prepare_document" or not isinstance(result, dict):
            return result
        if result.get("error") or result.get("missing_fields"):
            return result
        if self.agent.get("read_only") or not self.agent.get("allow_actions"):
            return result

        doc_type = result.get("doctype")
        payload = self._document_payload({
            "doctype": doc_type,
            "payload": result.get("payload") or {},
        })
        if not doc_type or not payload:
            return result

        approval = self._create_or_get_create_approval(doc_type, payload)

        enriched = dict(result)
        enriched.update({
            "approval_required": True,
            "approval_id": approval.name,
            "approval_status": approval.status,
        })
        return enriched

    def _create_or_get_create_approval(self, doc_type, payload):
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.run_name}:create:{doc_type}:{payload_hash}".encode()
        ).hexdigest()

        existing = frappe.db.exists("AI Approval Request", {"idempotency_key": idempotency_key})
        if not existing:
            existing = frappe.db.exists("AI Approval Request", {
                "run": self.run_name,
                "action_type": "Create",
                "doctype_name": doc_type,
                "payload_hash": payload_hash,
                "status": ("in", ["Pending", "Approved"]),
            })
        if existing:
            return frappe.get_doc("AI Approval Request", existing)

        approval = self.approval_engine.create_approval_request(
            run_name=self.run_name,
            action_type="Create",
            doctype_name=doc_type,
            action_summary=f"Create {doc_type} draft",
            impact_summary=f"A new {doc_type} will be created after approval.",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        self._record_approval_step(f"Approval required: Create {doc_type}", approval.name)
        return approval

    def _create_or_get_bulk_create_approval(self, documents, summary=None):
        payload = {"documents": documents}
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.run_name}:bulk-create:{payload_hash}".encode()
        ).hexdigest()

        existing = frappe.db.exists("AI Approval Request", {"idempotency_key": idempotency_key})
        if not existing:
            existing = frappe.db.exists("AI Approval Request", {
                "run": self.run_name,
                "action_type": "Bulk Create",
                "payload_hash": payload_hash,
                "status": ("in", ["Pending", "Approved"]),
            })
        if existing:
            return frappe.get_doc("AI Approval Request", existing)

        doctypes = sorted({document["doctype"] for document in documents})
        action_summary = summary or f"Create {len(documents)} draft documents"
        approval = self.approval_engine.create_approval_request(
            run_name=self.run_name,
            action_type="Bulk Create",
            doctype_name=doctypes[0] if len(doctypes) == 1 else None,
            action_summary=action_summary,
            impact_summary=f"{len(documents)} draft documents will be created after one approval.",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        self._record_approval_step(f"Approval required: Bulk create {len(documents)} documents", approval.name)
        return approval

    def _record_approval_step(self, title, approval_name):
        approval_step = self._add_step("Approval", title)
        self.trace.complete_step(approval_step.name, summary=f"Pending approval: {approval_name}")

    def _document_payload(self, arguments, include_doctype=True):
        payload = dict(arguments.get("payload") or {})
        if include_doctype and arguments.get("doctype"):
            payload["doctype"] = arguments.get("doctype")
        return payload

    def _bulk_create_documents(self, documents):
        if len(documents) > 100:
            raise ToolDenied("Bulk create supports up to 100 documents per approval.")
        normalized = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            payload = dict(document.get("payload") or {})
            doc_type = document.get("doctype") or payload.get("doctype")
            if not doc_type or not payload:
                continue
            payload["doctype"] = doc_type
            normalized.append({"doctype": doc_type, "payload": payload})
        return normalized

    def _get_tool_schemas(self):
        """Get tool schemas for the model."""
        schemas = []
        allowed_names = self._allowed_skill_tool_names()
        for tool in self.tool_registry.list_tools():
            if self.enforce_skill_tools and tool["name"] not in allowed_names:
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
        try:
            return bool(frappe.get_doc("AI Settings").get("enable_memory"))
        except Exception:
            return False

    def _check_limits(self):
        settings = frappe.get_doc("AI Settings")
        if not settings.enabled:
            raise MYSYSAgentError("Agent system is disabled.")
        max_steps = self.agent.maximum_steps or settings.maximum_steps or 12
        max_tool_calls = self.agent.maximum_tool_calls or settings.maximum_tool_calls or 25

    def _check_cancellation(self):
        self.run.reload()
        if self.run.cancel_requested:
            raise RunCancelled("Run was cancelled by user.")
        started_at = self.run.started_at or self.run.creation
        elapsed = frappe.utils.time_diff_in_seconds(frappe.utils.now_datetime(), started_at)
        if elapsed > run_timeout_seconds(agent=self.agent):
            raise RunTimedOut("Run exceeded the configured execution time limit.")

    def _add_step(self, step_type, title, summary=""):
        self.sequence += 1
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
            self._record_model_usage(response)
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
        if tool_calls:
            names = [
                (call.get("function") or {}).get("name", "")
                for call in tool_calls
            ]
            summary = f"Requested {len(tool_calls)} tool call(s): {', '.join(filter(None, names))}"
        elif message.get("content"):
            summary = f"Produced an answer ({len(str(message.get('content')))} characters)."
        else:
            summary = f"Returned no answer (finish reason: {choice.get('finish_reason') or 'unknown'})."
        self.trace.complete_step(step.name, summary=summary[:500])
        return response

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

    def _record_model_usage(self, response):
        if not response:
            return
        try:
            usage = self.provider.get_usage(response) or {}
        except Exception:
            return

        input_tokens = cint(usage.get("input_tokens"))
        output_tokens = cint(usage.get("output_tokens"))
        if not input_tokens and not output_tokens:
            return

        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

        try:
            self.run.reload()
            self.run.input_tokens = self.input_tokens
            self.run.output_tokens = self.output_tokens
            self.run.save(ignore_permissions=True)
            frappe.db.commit()
            self.trace.publish_realtime(REALTIME_EVENTS["run_updated"], self.run_name)
        except Exception:
            pass

    def _complete_run(self, final_answer):
        if not final_answer:
            raise MYSYSAgentError("The model did not produce a final answer.")

        final_answer = self._ensure_required_json_final(final_answer)
        final_answer = self._sanitize_final_answer(final_answer)
        failure_reason = self._structured_failure_reason(final_answer)
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

    def _structured_failure_reason(self, final_answer):
        payload = self._extract_json_object(final_answer)
        if not isinstance(payload, dict):
            return ""
        status = str(payload.get("status") or "").strip().lower()
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        if status == "failed":
            return str(errors[0] if errors else payload.get("message") or "The structured result reported failure.")
        if not errors:
            return ""
        ignored = {"questions", "warnings", "errors", "status", "reply_markdown", "ui"}
        substantive_keys = [
            key for key in self._current_skill_policy().get("required_output_keys") or []
            if key not in ignored
        ]
        if substantive_keys and not any(payload.get(key) for key in substantive_keys):
            return str(errors[0])
        return ""

    def _ensure_required_json_final(self, final_answer):
        if not self._requires_json_final():
            return final_answer
        parsed = self._extract_json_object(final_answer)
        if isinstance(parsed, dict):
            missing_keys = []
            for key in self._current_skill_policy().get("required_output_keys") or []:
                if key not in parsed:
                    parsed[key] = []
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
        return json.dumps(self._fallback_json_final_answer(final_answer), ensure_ascii=False, default=str)

    def _requires_json_final(self):
        if self._current_skill_policy().get("response_format") == "JSON Object":
            return True
        prompt = self.run.prompt or ""
        markers = (
            "Return one complete valid JSON object only",
            "Return compact valid JSON only",
            "The first character of the final answer must be `{`",
        )
        return any(marker in prompt for marker in markers)

    def _valid_required_json(self, payload):
        if not isinstance(payload, dict):
            return False
        required_keys = self._current_skill_policy().get("required_output_keys") or []
        return all(key in payload for key in required_keys)

    def _current_skill_policy(self):
        policies = [
            skill.get("runtime_policy") or {}
            for skill in self.effective_skills
            if skill.get("allow_implicit_invocation")
        ]
        policies.extend(skill.get("runtime_policy") or {} for skill in self.loaded_skills)

        response_format = ""
        required_output_keys = []
        instructions = []
        finalization_max_tokens = 0
        finalization_timeout = 0
        for policy in policies:
            if policy.get("response_format") == "JSON Object":
                response_format = "JSON Object"
            elif policy.get("response_format") and not response_format:
                response_format = policy.get("response_format")
            for key in policy.get("required_output_keys") or []:
                if key not in required_output_keys:
                    required_output_keys.append(key)
            instruction = str(policy.get("final_output_instruction") or "").strip()
            if instruction and instruction not in instructions:
                instructions.append(instruction)
            finalization_max_tokens = max(
                finalization_max_tokens,
                cint(policy.get("finalization_max_tokens")),
            )
            finalization_timeout = max(
                finalization_timeout,
                cint(policy.get("finalization_timeout")),
            )
        return {
            "response_format": response_format,
            "required_output_keys": required_output_keys,
            "finalization_max_tokens": finalization_max_tokens,
            "finalization_timeout": finalization_timeout,
            "final_output_instruction": "\n".join(instructions),
        }

    def _fallback_json_final_answer(self, final_answer):
        fallback = {
            "status": "Needs Review",
            "client_readable_summary": str(final_answer or "")[:4000],
            "questions": [],
            "warnings": [
                "لم يرجع الوكيل بيانات منظمة صالحة بالكامل، لذلك تم حفظ حالة واضحة قابلة للمراجعة بدل رد نجاح عام."
            ],
            "errors": ["تعذر إنتاج عقد JSON النهائي المطلوب من نتائج التشغيل المتاحة."],
        }
        for key in self._current_skill_policy().get("required_output_keys") or []:
            fallback.setdefault(key, [])
        return fallback

    def _agent_model(self):
        settings = frappe.get_doc("AI Settings")
        return self.agent.get("model") or settings.default_model

    def _extract_json_object(self, text):
        cleaned = str(text or "").strip()
        if not cleaned:
            return None
        if cleaned.startswith("```"):
            match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
            if match:
                cleaned = match.group(1).strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(cleaned[start:end + 1])
        except Exception:
            return None

    def _sanitize_final_answer(self, final_answer):
        text = str(final_answer or "")
        if self._requires_json_final() and self._extract_json_object(text) is not None:
            return text.strip()
        text = re.sub(r"(?is)<\s*\|\s*\|\s*DSML.*?(?:calls>|$)", "", text)
        cleaned_lines = []
        for line in text.splitlines():
            lowered = line.lower()
            if any(marker in lowered for marker in ("dsml", "toolcalls", "invoke name=", "parameter name=")):
                continue
            cleaned_lines.append(line)
        text = "\n".join(cleaned_lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
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
            pass

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
            pass

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
        self.memory.store_completed_run(self.run, final_answer)

    def _store_lesson_from_failed_run(self, error_summary):
        lesson = (
            f"Previous run failed. User prompt: {self.run.prompt}. "
            f"Error: {error_summary}"
        )
        self.memory.store_run_lesson(self.run, lesson)


def execute_run(run_name):
    """Entry point for background job."""
    orchestrator = Orchestrator(run_name)
    orchestrator.execute()


def resume_run(run_name, approval_name):
    """Resume a run after approval."""
    approval = frappe.get_doc("AI Approval Request", approval_name)
    if approval.status != "Approved":
        return

    engine = ApprovalEngine(PermissionContext(approval.requested_by))
    try:
        result = engine.execute_approved_action(approval_name)
    except Exception as e:
        frappe.log_error(f"Approval execution error: {str(e)}", "AI Agent")
        return

    run = frappe.get_doc("AI Run", run_name)
    run.status = RUN_STATUS_RUNNING
    run.save(ignore_permissions=True)
    frappe.db.commit()

    execute_run(run_name)
