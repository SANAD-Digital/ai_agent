import json

import frappe
from frappe.utils import cint

from ai_agent.runtime.file_context import FileContextService
from ai_agent.runtime.memory_service import MemoryService


MAX_SESSION_CONTEXT_CHARS = 10000
MAX_CHAIN_RUNS = 12
MAX_TEXT_CHARS = 1200


class SessionContextService:
    """Build compact context from earlier runs in the same chat session."""

    def build_context(self, run_doc):
        previous_runs = self.get_previous_runs(run_doc)
        sections = []
        run_names = [run_doc.name]

        if previous_runs:
            run_names.extend([run.name for run in previous_runs])
            sections.append(self._build_chat_history(previous_runs))
            file_context = FileContextService().build_context_for_runs(
                [run.name for run in previous_runs],
                title="Previously attached files in this chat",
            )
            if file_context:
                sections.append(file_context)

        whatsapp_context = self._build_whatsapp_request_context(run_names)
        if whatsapp_context:
            sections.append(whatsapp_context)

        memory_context = MemoryService().build_context(
            user=run_doc.user,
            agent=run_doc.agent,
            company=run_doc.company,
        )
        if memory_context:
            sections.append(memory_context)

        text = "\n\n".join(section for section in sections if section)
        if len(text) > MAX_SESSION_CONTEXT_CHARS:
            text = text[-MAX_SESSION_CONTEXT_CHARS:].lstrip()
            text = "[earlier session context truncated]\n" + text
        return text

    def _build_whatsapp_request_context(self, run_names):
        if not run_names or not frappe.db.exists("DocType", "AI WhatsApp Request"):
            return ""

        requests = frappe.get_all(
            "AI WhatsApp Request",
            filters={"ai_run": ("in", list(dict.fromkeys(run_names)))},
            fields=[
                "ai_run", "whatsapp_number", "chat_jid", "customer", "customer_name",
                "request_type", "status", "subject", "assigned_department",
                "meeting_requested", "creation",
            ],
            order_by="creation asc",
            page_length=len(run_names),
        )
        if not requests:
            return ""

        lines = [
            "Structured WhatsApp request memory for this run chain. Use it to identify the customer, channel, and prior business request without requiring those details in the prompt.",
        ]
        for request in requests[-MAX_CHAIN_RUNS:]:
            label = request.customer or request.customer_name or "unknown customer"
            lines.append(
                "- "
                f"run={request.ai_run}; customer={label}; whatsapp={request.whatsapp_number or ''}; "
                f"type={request.request_type or ''}; status={request.status or ''}; "
                f"department={request.assigned_department or ''}; meeting_requested={cint(request.meeting_requested)}; "
                f"subject={self._clean(request.subject, 220)}"
            )
        return "\n".join(lines)

    def get_previous_runs(self, run_doc):
        names = []
        current = run_doc.parent_run
        seen = set()

        while current and current not in seen and len(names) < MAX_CHAIN_RUNS:
            seen.add(current)
            row = frappe.db.get_value(
                "AI Run",
                current,
                ["name", "parent_run"],
                as_dict=True,
            )
            if not row:
                break
            names.append(row.name)
            current = row.parent_run

        names.reverse()
        if not names:
            return []

        rows = frappe.get_all(
            "AI Run",
            filters={"name": ("in", names)},
            fields=[
                "name", "agent", "company", "prompt", "status", "final_answer",
                "error_summary", "current_step", "tool_call_count", "creation",
            ],
            order_by="creation asc",
            page_length=len(names),
        )
        by_name = {row.name: row for row in rows}
        return [by_name[name] for name in names if name in by_name]

    def _build_chat_history(self, runs):
        lines = [
            "Conversation context from this chat. Use it to resolve follow-up requests, clarifications, pronouns, and references to uploaded files.",
            "Do not repeat previous failed attempts unless the user asks.",
        ]

        for index, run in enumerate(runs[-MAX_CHAIN_RUNS:], start=1):
            lines.append("")
            lines.append(f"Turn {index} [{run.status}]")
            lines.append(f"- User asked: {self._clean(self._user_prompt_text(run.prompt), 700)}")
            if run.final_answer:
                lines.append(f"- Assistant answered: {self._clean(self._assistant_answer_text(run.final_answer), MAX_TEXT_CHARS)}")
            elif run.error_summary:
                lines.append(f"- Assistant failed/error: {self._clean(run.error_summary, 700)}")
            else:
                lines.append("- Assistant result: no final answer yet.")
            if run.current_step or run.tool_call_count:
                lines.append(
                    f"- Execution: steps={cint(run.current_step)}, tools={cint(run.tool_call_count)}"
                )
            approval_lines = self._approval_context_for_run(run.name)
            if approval_lines:
                lines.extend(approval_lines)

        return "\n".join(lines)

    def _approval_context_for_run(self, run_name):
        approvals = frappe.get_all(
            "AI Approval Request",
            filters={"run": run_name},
            fields=[
                "name", "action_type", "doctype_name", "document_name", "action_summary",
                "status", "execution_result",
            ],
            order_by="creation asc",
            page_length=10,
        )
        if not approvals:
            return []

        lines = ["- Approvals/actions:"]
        for approval in approvals:
            result_text = ""
            if approval.execution_result:
                try:
                    result = json.loads(approval.execution_result)
                    if result.get("document_name"):
                        result_text = f" -> created/executed `{result.get('document_name')}`"
                except Exception:
                    result_text = f" -> {self._clean(approval.execution_result, 300)}"
            lines.append(
                "  - "
                f"{approval.status}: {approval.action_type} {approval.doctype_name or ''} "
                f"{approval.document_name or ''}{result_text}; "
                f"{self._clean(approval.action_summary, 300)}"
            )
        return lines

    def _clean(self, text, limit):
        value = " ".join(str(text or "").split())
        if len(value) > limit:
            return value[:limit].rstrip() + "…"
        return value

    def _user_prompt_text(self, prompt):
        text = str(prompt or "")
        marker = "رسالة العميل الحالية:"
        if marker in text:
            return text.split(marker, 1)[1].strip()
        return text.strip()

    def _assistant_answer_text(self, answer):
        text = str(answer or "").strip()
        lowered = text.lower()
        unsafe_markers = (
            "reached step limit",
            "data.query",
            "reports.search",
            "schema.describe",
            "الأدوات المنفذة",
            "السبب التقني",
        )
        if any(marker in lowered for marker in unsafe_markers):
            return "لم يتم استخدام الرد السابق كسياق لأنه يحتوي تفاصيل تشغيل داخلية."
        return text


def serialize_context_payload(payload):
    return json.dumps(payload, ensure_ascii=False, default=str)
