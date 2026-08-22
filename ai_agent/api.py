import frappe
from frappe import _
from frappe.utils import cint
import json
import base64
import os
import re
from ai_agent.runtime.skill_registry import SkillRegistry
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.model_provider import estimate_context_window_tokens
from ai_agent.runtime import local_answers
from ai_agent.runtime.run_control import enqueue_run, recover_run_if_orphaned
from frappe.utils.file_manager import save_file


TERMINAL_STATUSES = ("Completed", "Failed", "Cancelled", "Timed Out")
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".csv", ".json", ".txt", ".md", ".log", ".pdf", ".xlsx", ".xls", ".png", ".jpg", ".jpeg", ".webp",
}
CHAT_RUN_FIELDS = [
    "name", "agent", "company", "provider", "model", "prompt", "status", "final_answer",
    "error_summary", "parent_run", "creation", "modified", "started_at",
    "completed_at", "current_step", "tool_call_count", "input_tokens",
    "output_tokens",
]


@frappe.whitelist()
def create_run(agent, prompt, company=None, parent_run=None, idempotency_key=None, attachments=None,
               provider=None, model=None):
    """Create a new AI Run."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot create runs."))

    provider, model = _resolve_run_provider_and_model(provider, model, parent_run)

    settings = frappe.get_cached_doc("AI Settings")
    if not settings.enabled:
        frappe.throw(_("Agent system is disabled."))

    if not prompt or len(prompt) > 1000000:
        frappe.throw(_("Prompt must be between 1 and 1000,000 characters."))

    if not frappe.db.exists("AI Agent", {"name": agent, "enabled": 1}):
        frappe.throw(_("Agent not found or disabled."))

    agent_doc = frappe.get_doc("AI Agent", agent)
    if company and hasattr(agent_doc, "allowed_companies") and agent_doc.allowed_companies:
        allowed = [c.company for c in agent_doc.allowed_companies]
        if company not in allowed:
            frappe.throw(_("Company not allowed for this agent."))

    action_agent = _get_action_agent_for_prompt(prompt, agent_doc)
    if action_agent:
        agent = action_agent
        agent_doc = frappe.get_doc("AI Agent", agent)

    if idempotency_key:
        existing = frappe.db.exists("AI Run", {"idempotency_key": idempotency_key})
        if existing:
            return {
                "run_name": existing,
                "status": "already_exists",
                "compact_session_name": _agent_compact_session_name(agent_doc),
            }

    run = frappe.get_doc({
        "doctype": "AI Run",
        "agent": agent,
        "user": frappe.session.user,
        "company": company,
        "prompt": prompt,
        "status": "Queued",
        "parent_run": parent_run,
        "provider": provider,
        "model": model,
        "idempotency_key": idempotency_key or "",
        "started_at": frappe.utils.now_datetime(),
    })
    run.insert(ignore_permissions=True)
    saved_attachments = _save_run_attachments(run.name, attachments)

    local_answer = _local_fast_answer(prompt, parent_run=parent_run, has_attachments=bool(saved_attachments))
    if local_answer:
        run.status = "Completed"
        run.final_answer = local_answer
        run.current_step = 2
        run.tool_call_count = 0
        run.completed_at = frappe.utils.now_datetime()
        run.save(ignore_permissions=True)
        frappe.db.commit()
        return {
            "run_name": run.name,
            "status": "completed",
            "compact_session_name": _agent_compact_session_name(agent_doc),
        }

    frappe.db.commit()

    enqueue_run(run.name, frappe.session.user)

    return {
        "run_name": run.name,
        "status": "queued",
        "compact_session_name": _agent_compact_session_name(agent_doc),
        "execution_status": _run_execution_status(run),
    }


def _resolve_run_provider_and_model(provider, model, parent_run):
    """Explicit choice wins; a follow-up message inherits the parent's
    provider/model so a conversation stays on one model unless changed."""
    provider = str(provider or "").strip()
    model = str(model or "").strip()[:140]
    if not provider and not model and parent_run:
        parent = frappe.db.get_value("AI Run", parent_run, ["provider", "model"], as_dict=True)
        if parent:
            provider = parent.provider or ""
            model = parent.model or ""
    if provider:
        if not frappe.db.exists("AI Provider", {"name": provider, "enabled": 1}):
            frappe.throw(_("AI Provider not found or disabled."))
    return provider or None, model or None


@frappe.whitelist()
def list_available_providers():
    """Enabled AI Providers with their selectable models (no secrets)."""
    if not frappe.db.exists("DocType", "AI Provider"):
        return {"providers": []}
    providers = frappe.get_all(
        "AI Provider",
        filters={"enabled": 1},
        fields=["name", "provider_name", "provider_type", "default_model", "is_default", "description"],
        order_by="is_default desc, provider_name asc",
    )
    for provider in providers:
        rows = frappe.get_all(
            "AI Provider Model",
            filters={"parent": provider.name, "parenttype": "AI Provider"},
            fields=["model_name", "label", "is_default", "supports_vision", "context_window_tokens"],
            order_by="idx asc",
        )
        provider["models"] = rows
        if not provider.default_model:
            provider["default_model"] = next(
                (row.model_name for row in rows if row.is_default),
                rows[0].model_name if rows else "",
            )
    return {"providers": providers}


def _save_run_attachments(run_name, attachments):
    if not attachments:
        return []
    if isinstance(attachments, str):
        attachments = json.loads(attachments or "[]")
    if not isinstance(attachments, list):
        frappe.throw(_("Attachments must be a list."))
    if len(attachments) > MAX_ATTACHMENTS:
        frappe.throw(_("You can attach up to {0} files.").format(MAX_ATTACHMENTS))

    saved = []
    for attachment in attachments:
        filename = (attachment.get("filename") or "").strip()
        content_base64 = attachment.get("content_base64") or ""
        if not filename or not content_base64:
            continue

        extension = os.path.splitext(filename)[1].lower()
        if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
            frappe.throw(_("File type not allowed: {0}").format(extension or filename))

        try:
            content = base64.b64decode(content_base64.split(",", 1)[-1], validate=False)
        except Exception:
            frappe.throw(_("Could not decode attached file: {0}").format(filename))

        if len(content) > MAX_ATTACHMENT_BYTES:
            frappe.throw(_("File is too large: {0}").format(filename))

        file_doc = save_file(filename, content, "AI Run", run_name, is_private=1)
        saved.append({
            "name": file_doc.name,
            "file_name": file_doc.file_name,
            "file_size": file_doc.file_size,
        })
    return saved


@frappe.whitelist()
def list_chat_sessions(limit=30, include_archived=0):
    """List the user's chat sessions from the AI Chat Session doctype."""
    limit = cint(limit) or 30
    filters = _run_access_filters()
    if not cint(include_archived):
        filters["status"] = "Active"
    rows = frappe.get_all(
        "AI Chat Session",
        filters=filters,
        fields=[
            "name", "title", "user", "agent", "company", "status",
            "root_run", "last_run", "last_message_on", "message_count", "modified",
        ],
        order_by="last_message_on desc, modified desc",
        page_length=limit,
    )

    last_run_names = [row.last_run for row in rows if row.last_run]
    last_runs = {}
    if last_run_names:
        last_runs = {
            run.name: run
            for run in frappe.get_all(
                "AI Run",
                filters={"name": ["in", last_run_names]},
                fields=["name", "prompt", "status", "final_answer", "error_summary"],
                page_length=len(last_run_names),
            )
        }

    sessions = []
    for row in rows:
        last_run = last_runs.get(row.last_run)
        sessions.append({
            "name": row.name,
            "session": row.name,
            "root_run": row.root_run,
            "latest_run": row.last_run,
            "title": row.title or _truncate((last_run or {}).get("prompt"), 80) or row.name,
            "preview": _run_preview(last_run) if last_run else "",
            "status": (last_run or {}).get("status") or "",
            "session_status": row.status,
            "agent": row.agent,
            "company": row.company,
            "compact_session_name": _agent_compact_session_name(row.agent),
            "modified": row.last_message_on or row.modified,
            "message_count": cint(row.message_count),
        })
    return {"sessions": sessions}


def _assert_chat_session_access(session):
    if session.user != frappe.session.user and not _is_system_manager():
        frappe.throw(_("Not authorized to view this chat session."), frappe.PermissionError)


@frappe.whitelist()
def rename_chat_session(session, title):
    """Rename a chat session from the workspace sidebar."""
    doc = frappe.get_doc("AI Chat Session", session)
    _assert_chat_session_access(doc)
    title = " ".join(str(title or "").split())[:140]
    if not title:
        frappe.throw(_("Title is required."))
    doc.title = title
    doc.save(ignore_permissions=True)
    return {"name": doc.name, "title": doc.title}


@frappe.whitelist()
def archive_chat_session(session, archived=1):
    """Archive (or restore) a chat session; runs are kept, only hidden."""
    doc = frappe.get_doc("AI Chat Session", session)
    _assert_chat_session_access(doc)
    doc.status = "Archived" if cint(archived) else "Active"
    doc.save(ignore_permissions=True)
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def get_chat_messages(root_run):
    """Return messages for one chat session."""
    # Accepts either an AI Chat Session name or (legacy) any AI Run name.
    session_name = None
    if frappe.db.exists("AI Chat Session", root_run):
        session = frappe.get_doc("AI Chat Session", root_run)
        _assert_chat_session_access(session)
        session_name = session.name
        root_name = session.root_run
        chain = frappe.get_all(
            "AI Run",
            filters=_run_access_filters_with({"chat_session": session_name}),
            fields=CHAT_RUN_FIELDS,
            order_by="creation asc",
            page_length=1000,
        )
    else:
        requested = frappe.get_doc("AI Run", root_run)
        _assert_run_access(requested)
        session_name = requested.get("chat_session")
        root_name = requested.get("root_run") or _resolve_root_run_name(requested)
        chain = frappe.get_all(
            "AI Run",
            filters=_run_access_filters_with(
                {"chat_session": session_name} if session_name else {"root_run": root_name}
            ),
            fields=CHAT_RUN_FIELDS,
            order_by="creation asc",
            page_length=1000,
        )
    if not chain:
        # Legacy chains created before root_run was backfilled.
        chain = _get_chat_run_chain(root_name)
        chain.sort(key=lambda run: run.creation)

    messages = []
    for run in chain:
        artifacts = _get_artifact_previews(run.name)
        print_meta = _get_run_print_metadata(run, artifacts)
        messages.append({
            "role": "user",
            "run_name": run.name,
            "content": run.prompt,
            "created_at": run.creation,
            "attachments": _get_file_previews(run.name),
        })
        messages.append({
            "role": "assistant",
            "run_name": run.name,
            "content": _clean_answer_for_display(run.final_answer or run.error_summary or "", run.prompt),
            "user_prompt": run.prompt,
            "status": run.status,
            "agent": run.agent,
            "company": run.company,
            "compact_session_name": _agent_compact_session_name(run.agent),
            "created_at": run.completed_at or run.modified,
            "current_step": run.current_step,
            "tool_call_count": run.tool_call_count,
            "token_usage": _token_usage_for_run(run),
            "next_actions": _get_run_next_actions(run),
            "printable": print_meta["printable"],
            "print_title": print_meta["title"],
            "is_error": bool(run.error_summary and not run.final_answer),
            "artifacts": artifacts,
        })

    latest = chain[-1] if chain else None
    return {
        "session": session_name,
        "root_run": root_name,
        "latest_run": latest.name if latest else root_name,
        "latest_agent": latest.agent if latest else None,
        "latest_company": latest.company if latest else None,
        "latest_provider": latest.get("provider") if latest else None,
        "latest_model": latest.get("model") if latest else None,
        "compact_session_name": _agent_compact_session_name(latest.agent) if latest else "",
        "messages": messages,
    }


@frappe.whitelist()
def get_print_letter_heads():
    """Return enabled letter heads for the chat print dialog."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot print reports."))

    letter_heads = frappe.get_list(
        "Letter Head",
        filters={"disabled": 0},
        fields=["name", "letter_head_name", "is_default", "content", "footer"],
        order_by="is_default desc, letter_head_name asc",
        page_length=100,
    )
    return {
        "letter_heads": [
            {
                "name": item.name,
                "label": item.letter_head_name or item.name,
                "is_default": cint(item.is_default),
                "content": item.content or "",
                "footer": item.footer or "",
            }
            for item in letter_heads
        ]
    }


def _get_artifact_previews(run_name):
    artifacts = frappe.get_all(
        "AI Artifact",
        filters={"run": run_name},
        fields=["name", "artifact_type", "title", "summary", "row_count", "is_truncated", "content_json", "sequence"],
        order_by="sequence asc, creation asc",
        page_length=5,
    )
    previews = []
    for artifact in artifacts:
        content = {}
        try:
            content = json.loads(artifact.content_json or "{}")
        except Exception:
            content = {}
        rows = content.get("rows") if isinstance(content, dict) else []
        previews.append({
            "name": artifact.name,
            "artifact_type": artifact.artifact_type,
            "title": artifact.title,
            "summary": artifact.summary,
            "chart": content if artifact.artifact_type == "Chart" and isinstance(content, dict) else None,
            "row_count": artifact.row_count,
            "is_truncated": artifact.is_truncated,
            "columns": (content.get("columns") or [])[:30] if isinstance(content, dict) else [],
            "rows": (rows or [])[:50],
            "preview_row_count": min(len(rows or []), 50),
            "has_more": bool((artifact.row_count or 0) > min(len(rows or []), 50)),
        })
    return previews


@frappe.whitelist()
def get_artifact_content(artifact_name):
    """Return full artifact content for authorized run viewers."""
    artifact = frappe.get_doc("AI Artifact", artifact_name)
    run = frappe.get_doc("AI Run", artifact.run)
    _assert_run_access(run)

    try:
        content = json.loads(artifact.content_json or "{}")
    except Exception:
        content = {}

    rows = content.get("rows") if isinstance(content, dict) else []
    columns = content.get("columns") if isinstance(content, dict) else []
    return {
        "name": artifact.name,
        "chart": content if artifact.artifact_type == "Chart" and isinstance(content, dict) else None,
        "artifact_type": artifact.artifact_type,
        "title": artifact.title,
        "summary": artifact.summary,
        "row_count": artifact.row_count,
        "is_truncated": artifact.is_truncated,
        "columns": columns or [],
        "rows": rows or [],
    }


def _get_file_previews(run_name):
    files = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "AI Run",
            "attached_to_name": run_name,
            "is_folder": 0,
        },
        fields=["name", "file_name", "file_url", "file_size", "is_private"],
        order_by="creation asc",
        page_length=10,
    )
    return [
        {
            "name": file.name,
            "file_name": file.file_name,
            "file_url": file.file_url,
            "file_size": file.file_size,
            "is_private": file.is_private,
        }
        for file in files
    ]


def _get_run_print_metadata(run, artifacts=None):
    title = _truncate((run.prompt or "Report").replace("\n", " "), 90)
    if run.status != "Completed" or not run.final_answer or run.error_summary:
        return {"printable": False, "title": title}

    artifacts = artifacts if artifacts is not None else _get_artifact_previews(run.name)
    if artifacts:
        return {"printable": True, "title": title}

    tool_names = _get_run_tool_names(run.name)
    has_read_data = any(
        tool.startswith(("data.", "reports.", "artifacts."))
        for tool in tool_names
    )
    has_write_action = any(tool.startswith("actions.") for tool in tool_names)
    text = _normalize_prompt(f"{run.prompt or ''} {run.final_answer or ''}")
    has_table = bool(re.search(r"(?m)^\s*\|.+\|\s*$", run.final_answer or ""))
    asks_for_data = any(marker in text for marker in (
        "تقرير", "تحليل", "بحث", "بيانات", "كشف", "قائمه", "قائمة", "جدول",
        "مبيعات", "مشتريات", "عملاء", "موردين", "مخزون", "رصيد", "حساب",
        "report", "analysis", "search", "data", "table", "ledger",
    ))

    printable = (has_read_data or has_table or asks_for_data) and not (
        has_write_action and not (has_read_data or asks_for_data)
    )
    return {"printable": bool(printable), "title": title}


def _get_run_tool_names(run_name):
    try:
        return frappe.get_all(
            "AI Tool Call",
            filters={"run": run_name},
            pluck="tool_name",
            page_length=200,
        )
    except Exception:
        return []


def _get_run_next_actions(run):
    approvals = frappe.get_all(
        "AI Approval Request",
        filters={"run": run.name, "status": "Executed"},
        fields=["name", "action_type", "doctype_name", "document_name", "execution_result", "executed_at"],
        order_by="executed_at desc, modified desc",
        page_length=5,
    )
    actions = []
    for approval in approvals:
        for document in _approval_result_documents(approval):
            actions.extend(_next_actions_for_document(document.get("doctype"), document.get("document_name"), run.user))
            if actions:
                return actions[:2]
            continuation_action = _continuation_action_after_master(document.get("doctype"), run)
            if continuation_action:
                return [continuation_action]
    return actions


def _approval_result_documents(approval):
    documents = []
    try:
        result = json.loads(approval.execution_result or "{}")
    except Exception:
        result = {}

    for document in result.get("documents") or []:
        if document.get("doctype") and document.get("document_name"):
            documents.append(document)

    doctype_name = result.get("doctype") or approval.doctype_name
    document_name = result.get("document_name") or approval.document_name
    if doctype_name and document_name:
        documents.append({"doctype": doctype_name, "document_name": document_name})
    return documents


def _next_actions_for_document(doctype_name, document_name, user=None):
    if not doctype_name or not document_name:
        return []
    try:
        doc = frappe.get_doc(doctype_name, document_name)
    except Exception:
        return []

    user = user or frappe.session.user
    actions = []
    if (
        _is_submittable_doctype(doctype_name)
        and cint(getattr(doc, "docstatus", 0)) == 0
        and frappe.has_permission(doctype_name, "submit", doc=doc, user=user)
    ):
        actions.append({
            "label": _submit_action_label(doctype_name),
            "prompt": _submit_action_prompt(doctype_name, document_name),
            "kind": "submit",
            "doctype": doctype_name,
            "document_name": document_name,
        })
        return actions

    outstanding = float(getattr(doc, "outstanding_amount", 0) or 0)
    if cint(getattr(doc, "docstatus", 0)) == 1 and outstanding > 0 and frappe.has_permission("Payment Entry", "create", user=user):
        payment_action = _payment_action_for_invoice(doctype_name, document_name)
        if payment_action:
            actions.append(payment_action)
    return actions


def _is_submittable_doctype(doctype_name):
    try:
        return bool(getattr(frappe.get_meta(doctype_name), "is_submittable", False))
    except Exception:
        return False


def _continuation_action_after_master(doctype_name, run):
    if doctype_name not in {"Item", "Supplier", "Customer"}:
        return None

    source = _normalize_prompt(f"{run.prompt or ''} {run.final_answer or ''}")
    if "فاتوره شراء" in source or "فاتورة شراء" in source or "فاتوره مشتريات" in source or "فاتورة مشتريات" in source:
        return {
            "label": "أكمل إنشاء فاتورة الشراء",
            "prompt": "اكمل إنشاء فاتورة الشراء",
            "kind": "continue",
            "doctype": doctype_name,
        }
    if "فاتوره بيع" in source or "فاتورة بيع" in source:
        return {
            "label": "أكمل إنشاء فاتورة البيع",
            "prompt": "اكمل إنشاء فاتورة البيع",
            "kind": "continue",
            "doctype": doctype_name,
        }
    if doctype_name == "Item":
        return {
            "label": "أكمل تجهيز الأصناف",
            "prompt": "اكمل تجهيز الأصناف",
            "kind": "continue",
            "doctype": doctype_name,
        }
    return None


def _submit_action_label(doctype_name):
    labels = {
        "Purchase Invoice": "هل تريد تنفيذ اعتماد فاتورة الشراء؟",
        "Sales Invoice": "هل تريد تنفيذ اعتماد فاتورة البيع؟",
        "Purchase Order": "هل تريد تنفيذ اعتماد أمر الشراء؟",
        "Sales Order": "هل تريد تنفيذ اعتماد أمر البيع؟",
        "Payment Entry": "هل تريد تنفيذ اعتماد سند الدفع؟",
    }
    return labels.get(doctype_name, f"هل تريد تنفيذ اعتماد {doctype_name}؟")


def _submit_action_prompt(doctype_name, document_name):
    labels = {
        "Purchase Invoice": "اعتمد فاتورة الشراء",
        "Sales Invoice": "اعتمد فاتورة البيع",
        "Purchase Order": "اعتمد أمر الشراء",
        "Sales Order": "اعتمد أمر البيع",
        "Payment Entry": "اعتمد سند الدفع",
    }
    action = labels.get(doctype_name, f"اعتمد {doctype_name}")
    return f"{action} {document_name}"


def _payment_action_for_invoice(doctype_name, document_name):
    if doctype_name == "Purchase Invoice":
        return {
            "label": "هل تريد تنفيذ سجل دفع الفاتورة؟",
            "prompt": f"سجل دفع فاتورة الشراء {document_name}",
            "kind": "payment",
            "doctype": doctype_name,
            "document_name": document_name,
        }
    if doctype_name == "Sales Invoice":
        return {
            "label": "هل تريد تنفيذ سجل قبض الفاتورة؟",
            "prompt": f"سجل قبض فاتورة البيع {document_name}",
            "kind": "payment",
            "doctype": doctype_name,
            "document_name": document_name,
        }
    return None


@frappe.whitelist()
def get_run(run_name):
    """Get run details."""
    run = frappe.get_doc("AI Run", run_name)
    if run.user != frappe.session.user and not _is_system_manager():
        frappe.throw(_("Not authorized to view this run."), frappe.PermissionError)
    if recover_run_if_orphaned(run):
        run.reload()

    return {
        "name": run.name,
        "run_id": run.run_id,
        "agent": run.agent,
        "user": run.user,
        "company": run.company,
        "prompt": run.prompt,
        "status": run.status,
        "plan_summary": run.plan_summary,
        "final_answer": run.final_answer,
        "error_summary": run.error_summary,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "current_step": run.current_step,
        "tool_call_count": run.tool_call_count,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "compact_session_name": _agent_compact_session_name(run.agent),
        "token_usage": _token_usage_for_run(run),
        "execution_status": _run_execution_status(run) if run.status == "Queued" else None,
    }


def _token_usage_for_run(run):
    input_tokens = cint(run.get("input_tokens"))
    output_tokens = cint(run.get("output_tokens"))
    total_tokens = input_tokens + output_tokens
    model = None
    try:
        model = frappe.db.get_value("AI Agent", run.agent, "model")
    except Exception:
        model = None
    if not model:
        model = frappe.db.get_single_value("AI Settings", "default_model")
    context_window = estimate_context_window_tokens(model)
    remaining = max(context_window - total_tokens, 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "context_window_tokens": context_window,
        "remaining_context_tokens": remaining,
        "remaining_percent": round((remaining / context_window) * 100, 1) if context_window else 0,
        "is_estimated": True,
    }


def _run_execution_status(run):
    return {
        "mode": "background_queue",
        "state": "starting" if run.status == "Queued" else run.status,
        "message": _("يعمل الآن في طابور المهام الخلفية"),
    }


@frappe.whitelist()
def get_run_steps(run_name, start=0, page_length=50):
    """Get steps for a run."""
    run = frappe.get_doc("AI Run", run_name)
    if run.user != frappe.session.user and not _is_system_manager():
        frappe.throw(_("Not authorized."), frappe.PermissionError)

    steps = frappe.get_all(
        "AI Run Step",
        filters={"run": run_name, "is_user_visible": 1},
        fields=["name", "sequence", "step_type", "title", "summary", "status", "started_at", "completed_at", "duration_ms"],
        order_by="sequence asc",
        start=cint(start),
        page_length=cint(page_length),
    )
    return {"steps": steps}


@frappe.whitelist()
def get_run_artifacts(run_name):
    """Get artifacts for a run."""
    run = frappe.get_doc("AI Run", run_name)
    if run.user != frappe.session.user and not _is_system_manager():
        frappe.throw(_("Not authorized."), frappe.PermissionError)

    artifacts = frappe.get_all(
        "AI Artifact",
        filters={"run": run_name},
        fields=["name", "artifact_type", "title", "summary", "row_count", "is_truncated", "sequence"],
        order_by="sequence asc",
    )
    return {"artifacts": artifacts}


@frappe.whitelist()
def cancel_run(run_name):
    """Cancel a running AI Run."""
    run = frappe.get_doc("AI Run", run_name)
    if run.user != frappe.session.user and not _is_system_manager():
        frappe.throw(_("Not authorized."), frappe.PermissionError)

    if run.status in ("Completed", "Failed", "Cancelled"):
        frappe.throw(_("Run is already finished."))

    run.cancel_requested = 1
    run.save(ignore_permissions=True)
    frappe.db.commit()

    return {"status": "cancel_requested"}


@frappe.whitelist()
def list_available_agents():
    """List enabled agents available to the current user."""
    agents = frappe.get_all(
        "AI Agent",
        filters={"enabled": 1},
        fields=["name", "agent_name", "description", "read_only", "allow_actions", "compact_session"],
    )
    for agent in agents:
        agent["compact_session_name"] = _agent_compact_session_name(agent)
        agent["has_compact_session"] = bool(agent.get("compact_session"))
        agent.pop("compact_session", None)
    return {"agents": agents}


def _agent_compact_session_name(agent):
    if isinstance(agent, str):
        row = frappe.db.get_value("AI Agent", agent, ["agent_name", "compact_session"], as_dict=True) or {}
        agent_name = row.get("agent_name") or agent
        compact_session = row.get("compact_session") or ""
    else:
        agent_name = agent.get("agent_name") or agent.get("name") or "agent"
        compact_session = agent.get("compact_session") or ""
    explicit = _explicit_compact_session_name(compact_session)
    if explicit:
        return explicit
    return _slug_compact_session_name(agent_name)


def _explicit_compact_session_name(compact_session):
    for line in str(compact_session or "").splitlines():
        lowered = line.lower().strip()
        if lowered.startswith("session name"):
            _, _, value = line.partition(":")
            return _slug_compact_session_name(value)
    return ""


def _slug_compact_session_name(value):
    base = str(value or "agent").strip().lower()
    value = []
    previous_underscore = False
    for char in base:
        if char.isalnum():
            value.append(char)
            previous_underscore = False
        elif not previous_underscore:
            value.append("_")
            previous_underscore = True
    return "".join(value).strip("_") or "agent"


@frappe.whitelist()
def list_available_skills(agent, company=None):
    """List skills available for a given agent."""
    registry = SkillRegistry()
    skills = registry.list_effective_skills(frappe.session.user, agent, company)
    return {"skills": skills}


@frappe.whitelist()
def approve_action(approval_name, payload_hash=None):
    """Approve an action."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot approve actions."))

    ctx = PermissionContext(frappe.session.user)
    engine = ApprovalEngine(ctx)

    try:
        approval = engine.approve(approval_name, frappe.session.user)
        result = engine.execute_approved_action(approval.name)
        _append_approval_execution_to_run(approval, result)
        return {"status": "executed", "approval_id": approval.name, "result": result}
    except Exception as e:
        frappe.throw(str(e))


@frappe.whitelist()
def reject_action(approval_name, reason=None):
    """Reject an action."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot reject actions."))

    ctx = PermissionContext(frappe.session.user)
    engine = ApprovalEngine(ctx)

    try:
        approval = engine.reject(approval_name, reason, frappe.session.user)
        return {"status": "rejected", "approval_id": approval.name}
    except Exception as e:
        frappe.throw(str(e))


@frappe.whitelist()
def get_pending_approvals(run_name=None):
    """Get pending approval requests."""
    filters = {"status": "Pending"}
    if run_name:
        run = frappe.get_doc("AI Run", run_name)
        _assert_run_access(run)
        filters["run"] = run_name
    elif not _is_system_manager():
        user_runs = frappe.get_all("AI Run", filters={"user": frappe.session.user}, pluck="name")
        if not user_runs:
            return {"approvals": []}
        filters["run"] = ("in", user_runs)

    approvals = frappe.get_all(
        "AI Approval Request",
        filters=filters,
        fields=["name", "run", "action_type", "doctype_name", "document_name", "action_summary", "requested_by", "requested_at", "expires_at"],
        order_by="requested_at desc",
    )
    return {"approvals": approvals}


def _append_approval_execution_to_run(approval, result):
    if not approval.run:
        return
    try:
        run = frappe.get_doc("AI Run", approval.run)
    except Exception:
        return

    document_name = result.get("document_name") if isinstance(result, dict) else None
    documents = result.get("documents") if isinstance(result, dict) else None
    doctype_name = result.get("doctype") if isinstance(result, dict) else approval.doctype_name
    execution_note = (
        "\n\n---\n"
        "✅ **تم تنفيذ الموافقة**\n\n"
        f"- الإجراء: **{approval.action_type}**\n"
        f"- المستند: **{doctype_name or approval.doctype_name}**"
    )
    if documents:
        execution_note += f"\n- العدد: **{len(documents)}**"
        preview = ", ".join(
            str(document.get("document_name"))
            for document in documents[:8]
            if document.get("document_name")
        )
        if preview:
            execution_note += f"\n- المستندات: `{preview}`"
    if document_name:
        execution_note += f"\n- الرقم/الاسم: `{document_name}`"
    next_actions = _next_actions_for_document(doctype_name, document_name, run.user)
    continuation_action = _continuation_action_after_master(doctype_name, run)
    if next_actions:
        execution_note += f"\n\n**الخطوة التالية المقترحة:** {next_actions[0]['label']}"
    elif continuation_action:
        execution_note += f"\n\n**الخطوة التالية المقترحة:** {continuation_action['label']}"
    elif approval.action_type in ("Submit", "Cancel"):
        execution_note += "\n\n✅ **تم إكمال هذه الخطوة ولا توجد متابعة تلقائية مطلوبة لهذا المستند.**"
    else:
        execution_note += (
            "\n\nإذا كان هذا المستند جزءاً من طلب مركب، اضغط زر **أكمل الخطوة التالية** "
            "وسأتابع باستخدام نفس سياق الشات والملفات السابقة."
        )

    current_answer = run.final_answer or ""
    if document_name and document_name in current_answer and "تم تنفيذ الموافقة" in current_answer:
        return
    run.final_answer = f"{current_answer.rstrip()}{execution_note}" if current_answer else execution_note.strip()
    run.save(ignore_permissions=True)
    frappe.db.commit()


def _run_access_filters():
    if _is_system_manager():
        return {}
    return {"user": frappe.session.user}


def _run_access_filters_with(extra):
    filters = dict(_run_access_filters())
    filters.update(extra or {})
    return filters


def _assert_run_access(run):
    if run.user != frappe.session.user and not _is_system_manager():
        frappe.throw(_("Not authorized to view this run."), frappe.PermissionError)


def _is_system_manager():
    return "System Manager" in frappe.get_roles(frappe.session.user)


def _get_action_agent_for_prompt(prompt, agent_doc):
    if not _is_action_prompt(prompt):
        return None
    if agent_doc.get("allow_actions") and not agent_doc.get("read_only"):
        return None

    if _is_compound_action_prompt(prompt):
        power_agent = frappe.db.exists("AI Agent", "ERP Power Operator")
        if power_agent:
            return power_agent

    operations_agent = frappe.db.exists(
        "AI Agent",
        {"enabled": 1, "allow_actions": 1, "read_only": 0},
    )
    if operations_agent:
        return operations_agent

    frappe.throw(
        _("This request needs an operations agent. Enable an AI Agent with actions allowed."),
    )


def _is_action_prompt(prompt):
    text = _normalize_prompt(prompt)
    action_terms = (
        "انشاء", "إنشاء", "اضف", "إضافة", "اضافة", "سوي", "اعمل",
        "حدث", "تحديث", "عدل", "تعديل", "احذف", "حذف", "الغاء",
        "create", "add", "update", "edit", "delete", "cancel", "submit",
    )
    target_terms = (
        "todo", "to do", "task", "مهمه", "مهمة", "فاتوره", "فاتورة",
        "طلب", "عميل", "مورد", "مستند", "document",
    )
    return any(term in text for term in action_terms) and any(term in text for term in target_terms)


def _local_fast_answer(prompt, parent_run=None, has_attachments=False):
    if has_attachments:
        return None

    text = _normalize_prompt(prompt)
    if not text:
        return None

    # Action-looking prompts must always reach the model — even when they
    # mention a previous error ("صحح الخطأ في الفاتورة" is a request, not a
    # question about the failure).
    if any(marker in text for marker in local_answers.ACTION_MARKERS):
        return None

    failure_answer = _local_failure_answer(text, parent_run)
    if failure_answer:
        return failure_answer

    return local_answers.greeting_or_capability_answer(prompt)


def _local_failure_answer(text, parent_run=None):
    failure_markers = (
        "ليش فشل", "ليش فشلت", "لماذا فشل", "لماذا فشلت", "سبب الفشل",
        "ايش الخطا", "ما الخطا", "ماهو الخطا", "وش الخطا",
        "why failed", "why did it fail", "what error",
    )
    if not any(marker in text for marker in failure_markers):
        return None

    failed_run = _get_related_failed_run(parent_run)
    if not failed_run:
        return "لا أجد طلبًا سابقًا فاشلًا في هذه المحادثة أشرح سببه."

    error = failed_run.error_summary or failed_run.final_answer or "لم يتم تسجيل تفاصيل الخطأ."
    answer = (
        f"فشل الطلب السابق **{failed_run.name}** بسبب:\n\n"
        f"`{error}`\n\n"
    )
    if "api.deepseek.com" in error or "Model provider error" in error:
        answer += (
            "هذا يعني أن الطلب توقف عند مزود الموديل قبل تنفيذ الأدوات. "
            "راجعت توافق DeepSeek وعدّلت الإرسال حتى لا نرسل معاملات غير مدعومة مثل "
            "`thinking` أو `reasoning_effort` أو `strict` إلى واجهة DeepSeek."
        )
    else:
        answer += "لم أستخدم أدوات أو موديل لهذا الرد؛ هذا شرح مباشر من سجل الطلب السابق."
    return answer


def _get_related_failed_run(parent_run=None):
    if parent_run:
        try:
            run = frappe.get_doc("AI Run", parent_run)
            _assert_run_access(run)
            if run.status == "Failed" or run.error_summary:
                return run
        except Exception:
            pass

    filters = _run_access_filters()
    filters["status"] = "Failed"
    latest = frappe.get_all(
        "AI Run",
        filters=filters,
        fields=["name"],
        order_by="modified desc",
        page_length=1,
    )
    if latest:
        return frappe.get_doc("AI Run", latest[0].name)
    return None


def _is_compound_action_prompt(prompt):
    text = _normalize_prompt(prompt)
    compound_markers = (
        "كثير", "كبير", "كبيره", "متعدد", "متعدده", "اكثر من",
        "غير موجود", "مش موجود", "اذا مش", "ثم", "وبعدين", "بعدها",
        "مع بعض", "في طلب واحد", "ملف", "مرفق", "اصناف", "بنود",
        "many", "large", "multiple", "bulk", "batch", "if missing", "then",
    )
    document_chain_markers = (
        "فاتوره", "مستند", "عميل", "مورد", "صنف", "اصناف", "سند", "دفع", "قبض",
        "invoice", "document", "customer", "supplier", "item", "payment",
    )
    marker_count = sum(1 for marker in compound_markers if marker in text)
    chain_count = sum(1 for marker in document_chain_markers if marker in text)
    return marker_count >= 1 and chain_count >= 2


def _run_timeout_seconds(settings, agent_doc):
    base_timeout = cint(settings.maximum_run_seconds) or 600
    if not agent_doc:
        return base_timeout
    steps = cint(agent_doc.maximum_steps)
    tools = cint(agent_doc.maximum_tool_calls)
    if steps >= 25 or tools >= 50:
        return max(base_timeout, min(1800, max(steps * 30, tools * 15)))
    return base_timeout


def _normalize_prompt(prompt):
    return (
        (prompt or "").lower()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
    )


def _find_root_run(run, by_name):
    seen = set()
    current = run
    while current and current.parent_run and current.parent_run not in seen:
        seen.add(current.name)
        parent = by_name.get(current.parent_run)
        if not parent:
            return current.parent_run
        current = parent
    return current.name if current else run.name


def _resolve_root_run_name(run):
    seen = set()
    current = run
    while current and current.parent_run and current.parent_run not in seen:
        seen.add(current.name)
        if not frappe.db.exists("AI Run", current.parent_run):
            break
        parent = frappe.get_doc("AI Run", current.parent_run)
        _assert_run_access(parent)
        current = parent
    return current.name if current else run.name


def _get_chat_run_chain(root_name):
    runs_by_name = {}
    processed = set()
    frontier = [root_name]

    while frontier and len(runs_by_name) < 1000:
        current_names = [name for name in frontier if name not in processed]
        frontier = []
        if not current_names:
            continue
        processed.update(current_names)

        current_runs = frappe.get_all(
            "AI Run",
            filters=_run_access_filters_with({"name": ["in", current_names]}),
            fields=CHAT_RUN_FIELDS,
            page_length=len(current_names),
        )
        for run in current_runs:
            runs_by_name[run.name] = run

        child_runs = frappe.get_all(
            "AI Run",
            filters=_run_access_filters_with({"parent_run": ["in", current_names]}),
            fields=CHAT_RUN_FIELDS,
            order_by="creation asc",
            page_length=1000,
        )
        for child in child_runs:
            if child.name not in runs_by_name:
                runs_by_name[child.name] = child
            if child.name not in processed:
                frontier.append(child.name)

    return list(runs_by_name.values())


def _run_preview(run):
    if run.final_answer:
        return _truncate(_clean_answer_for_display(run.final_answer, run.prompt), 120)
    if run.error_summary:
        return _truncate(_clean_answer_for_display(run.error_summary, run.prompt), 120)
    return _truncate(run.prompt, 120)


def _clean_answer_for_display(answer, prompt=None):
    text = str(answer or "")
    text = re.sub(r"(?is)<\s*\|\s*\|\s*DSML.*?(?:calls>|$)", "", text)
    lines = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ("dsml", "toolcalls", "invoke name=", "parameter name=")):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = _normalize_report_answer_for_display(text)
    if _wants_filters_and_table_only(prompt):
        text = _strip_to_filters_and_tables(text) or text
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _normalize_report_answer_for_display(text):
    if not text or not _looks_like_report_answer(text):
        return text.strip()

    text = re.sub(r"(?is)</?\s*div\b[^>]*>", "", text)
    text = _strip_report_preamble(text)
    return text.strip()


def _looks_like_report_answer(text):
    normalized = _normalize_prompt(text)
    if not any(marker in normalized for marker in (
        "كشف", "تقرير", "تحليل", "اجماليات", "ارصده", "الرصيد",
        "جدول", "بيانات", "ملخص", "report", "analysis", "summary",
    )):
        return False
    return bool(re.search(r"(?m)^\s{0,3}(?:#{1,6}\s+|\|.+\||<table\b)", text, re.I))


def _strip_report_preamble(text):
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)

    chatter_starts = (
        "سأقدم", "ساقدم", "سوف أقدم", "سوف اقدم", "البيانات جاهزة",
        "الان لدي", "الآن لدي", "دعوني", "لدي جميع البيانات", "تم تجهيز",
        "here is", "i will", "i'll", "now i have",
    )

    while lines:
        current = lines[0].strip()
        normalized = _normalize_prompt(current)
        if not current or current in ("---", "***"):
            lines.pop(0)
            continue
        if any(normalized.startswith(_normalize_prompt(marker)) for marker in chatter_starts):
            lines.pop(0)
            continue
        break

    return "\n".join(lines)


def _wants_filters_and_table_only(prompt):
    normalized = _normalize_prompt(prompt or "")
    if not normalized:
        return False
    wants_filters = any(marker in normalized for marker in (
        "الفلاتر فقط", "فلاتر فقط", "اريد الفلاتر", "ابي الفلاتر",
        "filters only", "filter only",
    ))
    wants_table = any(marker in normalized for marker in (
        "الجدول", "جدول", "table",
    ))
    rejects_extra = any(marker in normalized for marker in (
        "لا ترد باي كلام", "لا ترد بأي كلام", "بدون كلام", "ما اريده",
        "ما اريد", "لا اريد", "لا تظهر", "بدون ملاحظات", "no notes",
        "only",
    ))
    return wants_filters and wants_table and rejects_extra


def _strip_to_filters_and_tables(text):
    lines = text.splitlines()
    output = []
    table_started = False
    table_finished = False
    kept_filters = set()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if table_started and not table_finished:
                output.append(line)
            continue

        if _is_markdown_table_line(stripped):
            table_started = True
            output.append(line)
            continue

        if table_started:
            table_finished = True
            continue

        filter_line = _extract_filter_only_line(stripped)
        if filter_line and filter_line not in kept_filters:
            kept_filters.add(filter_line)
            output.append(filter_line)

    has_table = any(_is_markdown_table_line(line.strip()) for line in output)
    return "\n".join(output).strip() if has_table else ""


def _is_markdown_table_line(line):
    return bool(re.match(r"^\|.*\|\s*$", line or ""))


def _extract_filter_only_line(line):
    normalized = _normalize_prompt(line)
    if any(marker in normalized for marker in (
        "المصدر", "source", "ملاحظه", "ملاحظات", "note", "notes",
    )):
        return None

    filter_markers = (
        "الفتره", "الفترة", "الشركه", "الشركة", "الشركات", "الشركتان",
        "العمله", "العملة", "من", "الى", "إلى", "الي", "from", "to",
        "company", "currency", "period", "filter",
    )

    segments = [segment.strip() for segment in re.split(r"\s*\|\s*", line) if segment.strip()]
    if len(segments) > 1:
        kept = [_filter_segment_text(segment) for segment in segments]
        kept = [segment for segment in kept if segment]
        return " | ".join(kept) if kept else None

    single_filter = _filter_segment_text(line)
    if single_filter:
        return single_filter
    if any(marker in normalized for marker in filter_markers) and not any(marker in normalized for marker in ("كشف", "تقرير", "تحليل")):
        return line
    return None


def _filter_segment_text(segment):
    normalized = _normalize_prompt(segment)
    if any(marker in normalized for marker in ("المصدر", "source", "ملاحظه", "ملاحظات", "note", "notes")):
        return None

    labels = (
        "**الفترة", "الفترة", "الفتره",
        "**الشركتان", "الشركتان",
        "**الشركات", "الشركات",
        "**الشركة", "الشركة", "الشركه",
        "**العملة", "العملة", "العمله",
        "**الفلاتر", "الفلاتر",
        "period", "company", "currency", "filters",
    )
    lowered = segment.lower()
    positions = [lowered.find(label.lower()) for label in labels if lowered.find(label.lower()) >= 0]
    if not positions:
        return None

    clean = segment[min(positions):].strip()
    clean = re.sub(r"^[#\-\s:]+", "", clean).strip()
    return clean


def _truncate(text, length):
    text = " ".join((text or "").split())
    length = cint(length) or 80
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"
