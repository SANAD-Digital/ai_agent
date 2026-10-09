import base64
import hashlib
import json
import os
import re
import time

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate
from frappe.utils.file_manager import save_file

from ai_agent.runtime.file_context import FileContextService, TEXT_DOCUMENT_EXTENSIONS
from ai_agent.runtime.orchestrator import execute_run
from ai_agent.runtime.skill_registry import SkillRegistry
from ai_agent.runtime.trace_service import TraceService
from ai_agent.runtime.run_control import mark_interrupted_run, run_job_id
from ai_agent.permissions import assert_owned_access
from ai_agent.purchase_invoice_ai.payloads import (  # noqa: F401 -- legacy re-exports
    MAX_ANALYSIS_ROW_TEXT_CHARS,
    analysis_failed_payload as _analysis_failed_payload,
    analysis_groups as _analysis_groups,
    analysis_has_extractable_data as _analysis_has_extractable_data,
    analysis_payload_score as _analysis_payload_score,
    analysis_row_count as _analysis_row_count,
    canonical_journal_line as _canonical_journal_line,
    canonicalize_journal_payload as _canonicalize_journal_payload,
    compact_json as _compact_json,
    compact_payload_text_fields as _compact_payload_text_fields,
    extract_json as _extract_json,
    first_analysis_error as _first_analysis_error,
    group_matches_fixed_journal_contract as _group_matches_fixed_journal_contract,
    is_plausible_document_number as _is_plausible_document_number,
    merge_page_chunk_payloads as _merge_page_chunk_payloads,
    model_message_content as _model_message_content,
    normalize_analysis_payload as _normalize_analysis_payload,
    payload_has_journal_rows as _payload_has_journal_rows,
    payload_shape as _payload_shape,
    primary_rows as _primary_rows,
    truncate_for_analysis as _truncate_for_analysis,
    valid_document_number as _valid_document_number,
)


PURCHASE_AGENT = "Purchase Invoice AI"
FINANCIAL_ATTACHMENTS_SKILL = "financial-attachments-to-journal-entry"
PURCHASE_SKILLS = [
    "core-agent-behavior",
    "accounting-memory-to-account",
    FINANCIAL_ATTACHMENTS_SKILL,
    "validate-journal-entry-proposal",
]
LEGACY_PURCHASE_INVOICE_SKILLS = [
    "analyze_purchase_invoice_bundle",
    "classify_purchase_invoice_attachments",
    "create_purchase_invoice_from_analysis",
    "match_purchase_invoice_items",
    "match_purchase_invoice_supplier",
    "validate_purchase_invoice_analysis",
    "frappe-schema-discovery",
    "document-creation",
    "compound-document-operations",
    "whatsapp-business-support",
]
TERMINAL_STATUSES = {"Completed", "Failed", "Cancelled", "Timed Out"}
MAX_FILES = 30
MAX_FILE_BYTES = 25 * 1024 * 1024
LOCAL_OCR_ENGINE = "Local Engine (PaddleOCR / PyMuPDF)"
ALLOWED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".webp", ".xlsx", ".xls", ".csv",
    ".xml", ".json", ".txt", ".docx",
}


@frappe.whitelist()
def bootstrap():
    """Return page bootstrap data and ensure the page-specific agent exists."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot use this page."))
    _ensure_purchase_invoice_agent()
    return {
        "agent": PURCHASE_AGENT,
        "companies": frappe.get_list("Company", fields=["name", "default_currency"], order_by="name asc"),
        "today": nowdate(),
        "max_files": MAX_FILES,
        "max_file_mb": int(MAX_FILE_BYTES / (1024 * 1024)),
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        "ui_contract": _skill_ui_contract(),
    }


@frappe.whitelist()
def start_extraction(company, files, grouping_mode="auto", user_instructions="", defaults=None):
    from ai_agent.purchase_invoice_ai.ocr_session import start_extraction as service

    return service(company, files, grouping_mode=grouping_mode, user_instructions=user_instructions, defaults=defaults)


@frappe.whitelist()
def read_selected_pages(run_name, pages):
    from ai_agent.purchase_invoice_ai.ocr_session import read_selected_pages as service

    return service(run_name, pages)


@frappe.whitelist()
def get_session(session_name):
    from ai_agent.purchase_invoice_ai.ocr_session import get_session as service

    return service(session_name)


@frappe.whitelist()
def set_selected_pages(session_name, pages):
    from ai_agent.purchase_invoice_ai.ocr_session import set_selected_pages as service

    return service(session_name, pages)


@frappe.whitelist()
def get_page_preview(session_name, page_id):
    from ai_agent.purchase_invoice_ai.ocr_session import get_page_preview as service

    return service(session_name, page_id)


def _check_and_handle_run_timeout(run):
    """Auto-fail stale runs while allowing long OCR page-reading jobs."""
    if run.status in ("Planning", "Queued", "Running"):
        started = run.started_at or run.creation
        if started:
            elapsed = (frappe.utils.now_datetime() - frappe.utils.get_datetime(started)).total_seconds()
            is_attachment_workflow = bool(_session_for_run(run.name) or run.parent_run)
            timeout_seconds = 1800 if is_attachment_workflow else 180
            if elapsed > timeout_seconds:
                run.status = "Failed"
                run.error_summary = _("استغرق التحليل وقتاً أطول من المتوقع وتوقف الطلب. يرجى إعادة المحاولة.")
                run.completed_at = frappe.utils.now_datetime()
                _safe_save_run(run)
                frappe.db.commit()


@frappe.whitelist()
def get_analysis(run_name):
    """Return run status plus parsed attachment reading or processing JSON."""
    run = frappe.get_doc("AI Run", run_name)
    _assert_run_access(run)
    _check_and_handle_run_timeout(run)
    final_text = run.final_answer or ""
    parsed = _extract_json(final_text)
    if not run.parent_run:
        # Status polling must only read persisted state. OCR belongs to queued jobs;
        # invoking it here can hold a web worker until Gunicorn kills the request.
        parsed = _attachment_reading_from_session(run.name) or parsed
    if not parsed and run.parent_run:
        parsed = _analysis_from_parent_run(run)
    is_model_chunk_payload = _is_model_chunk_analysis_payload(parsed)
    if parsed and run.parent_run and not is_model_chunk_payload and not _analysis_row_count(parsed):
        parsed = _analysis_from_parent_run(run) or parsed
    analysis_failed = bool(isinstance(parsed, dict) and (parsed.get("workflow") or {}).get("analysis_failed"))
    if parsed and run.parent_run and not analysis_failed and not is_model_chunk_payload:
        parsed = _postprocess_purchase_analysis(run, parsed)
    if parsed and not analysis_failed:
        parsed = _canonicalize_journal_payload(parsed, allow_page_fallback=False)
        parsed = _prepare_journal_payload_for_review(parsed, run.company)
    session = _session_for_run(run.parent_run or run.name)
    return {
        "run": {
            "name": run.name,
            "status": run.status,
            "parent_run": run.parent_run,
            "error_summary": run.error_summary,
            "final_answer": final_text,
            "current_step": run.current_step,
            "tool_call_count": run.tool_call_count,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
        },
        "analysis": parsed,
        "files": _run_files(run.name),
        "steps": _run_steps(run.name),
        "session": _session_payload(session) if session else None,
    }


def _is_model_chunk_analysis_payload(payload):
    if not isinstance(payload, dict):
        return False
    workflow = payload.get("workflow") or {}
    return isinstance(workflow, dict) and workflow.get("stage") == "model_page_chunk_analysis"


@frappe.whitelist()
def prepare_journal_entry_analysis(company, analysis):
    """Return the model-produced Journal Entry analysis without local guessing."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot prepare Journal Entry lines."))
    _assert_ai_enabled()
    _ensure_purchase_invoice_agent()

    payload = frappe.parse_json(analysis or "{}")
    if not isinstance(payload, dict):
        frappe.throw(_("Invalid analysis payload."))

    payload = _canonicalize_journal_payload(payload, allow_page_fallback=False)
    payload = _compact_payload_text_fields(payload)
    return {"analysis": payload, "warnings": payload.get("warnings") or [], "status": "completed"}


def _prepare_journal_payload_for_review(payload, company):
    """Do not enrich or guess locally; the model owns accounting suggestions."""
    return payload


@frappe.whitelist()
def start_discussion(company, message, active_step, analysis=None, source_run_name=None, current_run_name=None):
    """Ask the page agent to answer a workspace chat message and return a decision."""
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot discuss analysis."))
    _assert_ai_enabled()
    _ensure_purchase_invoice_agent()

    payload = frappe.parse_json(analysis or "{}")
    if payload and not isinstance(payload, dict):
        frappe.throw(_("Invalid analysis payload."))
    if not _analysis_row_count(payload):
        payload = _latest_discussion_analysis(payload, current_run_name, source_run_name)
    text = str(message or "").strip()
    if not text:
        frappe.throw(_("Message is required."))

    _assert_company_access(company)
    parent_run = current_run_name or source_run_name or None
    if parent_run:
        parent = frappe.get_doc("AI Run", parent_run)
        _assert_run_owner(parent)
    run = frappe.get_doc({
        "doctype": "AI Run",
        "agent": PURCHASE_AGENT,
        "user": frappe.session.user,
        "company": company,
        "prompt": _build_workspace_discussion_prompt(
            company=company,
            message=text,
            active_step=active_step,
            analysis=payload,
            source_run_name=source_run_name,
            current_run_name=current_run_name,
        ),
        "status": "Queued",
        "parent_run": parent_run,
        "started_at": frappe.utils.now_datetime(),
        "idempotency_key": "purchase_invoice_ai_discussion:" + hashlib.sha256(
            f"{frappe.session.user}|{frappe.utils.now_datetime()}|{text}".encode("utf-8")
        ).hexdigest(),
    })
    run.insert(ignore_permissions=True)
    frappe.db.commit()
    _start_background_run(run.name, frappe.session.user, getattr(frappe.local, "site", None))
    return {"run_name": run.name, "status": "queued"}


@frappe.whitelist()
def get_discussion(run_name):
    """Return the final answer for a workspace discussion run."""
    run = frappe.get_doc("AI Run", run_name)
    _assert_run_access(run)
    parsed = _extract_json(run.final_answer or "") if run.status == "Completed" else None
    if not isinstance(parsed, dict):
        parsed = {}
    reply = (
        parsed.get("reply_markdown")
        or parsed.get("reply")
        or (run.final_answer if run.status == "Completed" else "")
        or ""
    )
    return {
        "run": {
            "name": run.name,
            "status": run.status,
            "error_summary": run.error_summary,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "tool_call_count": run.tool_call_count,
        },
        "reply_markdown": reply,
        "decision": parsed.get("decision") if isinstance(parsed.get("decision"), dict) else {},
    }


@frappe.whitelist()
def cancel_analysis(run_name):
    run = frappe.get_doc("AI Run", run_name)
    _assert_run_owner(run)
    if run.status in TERMINAL_STATUSES:
        return {"status": run.status}
    frappe.db.set_value(
        "AI Run",
        run.name,
        {
            "cancel_requested": 1,
            "status": "Cancelled",
            "error_summary": _("تم إيقاف العملية بناءً على طلب المستخدم."),
            "completed_at": frappe.utils.now_datetime(),
        },
        update_modified=False,
    )
    _cancel_page_reading_session(run.name)
    frappe.db.commit()
    TraceService().publish_realtime("ai_agent:run_updated", run.name)
    return {"status": "Cancelled"}


def _build_compact_context(company):
    """Build pre-loaded compact context (leaf expense accounts, cash suppliers, cost centers, AI memory, metadata) for instant model decisions."""
    # 1. Leaf Expense Accounts (is_group = 0)
    account_filters = {"is_group": 0}
    if company:
        account_filters["company"] = company
    accounts = frappe.get_all(
        "Account",
        filters=account_filters,
        fields=["name", "account_name", "account_number", "account_type", "root_type"],
        limit=300,
    )
    leaf_expense_accounts = [
        {"name": a["name"], "account_name": a["account_name"], "number": a.get("account_number")}
        for a in accounts
        if a.get("account_type") in ("Expense Account", "Cost of Goods Sold") or a.get("root_type") == "Expense"
    ] or [{"name": a["name"], "account_name": a["account_name"]} for a in accounts[:50]]

    # 2. Existing Cash Suppliers
    cash_suppliers = []
    if frappe.db.exists("DocType", "Cash Supplier"):
        meta = frappe.get_meta("Cash Supplier")
        tax_field = _cash_supplier_tax_field(meta)
        mobile_field = _cash_supplier_mobile_field(meta)
        fields = ["name", "supplier_name"]
        if tax_field:
            fields.append(tax_field)
        if mobile_field:
            fields.append(mobile_field)
        raw_cs = frappe.get_all("Cash Supplier", fields=fields, limit=300)
        for cs in raw_cs:
            item = {"name": cs["name"], "supplier_name": cs.get("supplier_name")}
            if tax_field and cs.get(tax_field):
                item["tax_id"] = cs.get(tax_field)
            if mobile_field and cs.get(mobile_field):
                item["mobile_no"] = cs.get(mobile_field)
            cash_suppliers.append(item)

    # 3. Cost Centers
    cost_center_filters = {"is_group": 0}
    if company:
        cost_center_filters["company"] = company
    cost_centers = frappe.get_all("Cost Center", filters=cost_center_filters, fields=["name", "cost_center_name"], limit=30)

    # 4. AI Memory for Agent & Company
    memories = []
    if frappe.db.exists("DocType", "AI Memory"):
        raw_mem = frappe.get_all(
            "AI Memory",
            filters={"agent": PURCHASE_AGENT, "scope": "Agent"},
            fields=["memory_type", "content"],
            order_by="modified desc",
            limit=30,
        )
        for m in raw_mem:
            if m.get("content"):
                memories.append(m.get("content"))

    # 5. Metadata Schema hints
    schema_hints = {
        "doctype_journal_entry": ["company", "posting_date", "user_remark", "accounts"],
        "journal_entry_account_row": ["account", "debit_in_account_currency", "credit_in_account_currency", "party_type", "party", "custom_cash_supplier", "custom_tax_id", "custom_mobile_no", "cost_center"],
        "doctype_cash_supplier": ["supplier_name", "tax_id", "mobile_no"],
    }

    return {
        "company": company,
        "leaf_expense_accounts": leaf_expense_accounts[:150],
        "cash_suppliers": cash_suppliers[:150],
        "cost_centers": cost_centers[:30],
        "ai_memories": memories[:30],
        "schema_hints": schema_hints,
    }


def _ensure_purchase_invoice_agent():
    SkillRegistry().sync_app_skills()
    agent_name = frappe.db.exists("AI Agent", PURCHASE_AGENT)
    compact_session_base = (
        "Session purpose: analyze OCR evidence and prepare Journal Entry proposals when requested.\n"
        "Session name: journal_entry_from_ocr_v1\n"
        "Keep the current request and its OCR evidence separate from prior runs.\n"
        "If a prompt contains a Financial Attachment Session name, read its OCR text and produce the final invoice_groups JSON without asking for task clarification.\n"
        "For Journal Entry proposals, answer with the invoice_groups JSON contract defined by the granted skill."
    )
    values = {
        "description": "Specialized OCR-to-Journal Entry suggestion agent",
        "enabled": 1,
        "is_default": 0,
        "read_only": 0,
        "allow_actions": 1,
        "require_approval_for_all_writes": 0,
        "reasoning_effort": "medium",
        "maximum_steps": 100,
        "maximum_tool_calls": 25,
        "system_instructions": (
            "You analyze financial OCR evidence and propose ERPNext Journal Entries. "
            "When the prompt names a Financial Attachment Session, the task is to read that session and return reviewable Journal Entry proposal JSON; do not ask what the task is. "
            "Use financial_attachments.get_session_ocr_text for session OCR and narrow data.query calls on Account for exact leaf account names when account selection is needed. "
            "Invoices, receipts, and vouchers are source evidence; do not turn the request into Purchase Invoice creation unless the user explicitly asks for that. "
            "For a Journal Entry proposal, return the invoice_groups JSON contract defined by the financial-attachments-to-journal-entry skill."
        ),
        "compact_session": compact_session_base,
    }
    if agent_name:
        agent = frappe.get_doc("AI Agent", agent_name)
        checkpoint_marker = "<!-- AI_AGENT_RUNTIME_CHECKPOINT_START -->"
        current_compact_session = str(agent.get("compact_session") or "")
        if checkpoint_marker in current_compact_session:
            checkpoint = current_compact_session[current_compact_session.index(checkpoint_marker):].strip()
            values["compact_session"] = f"{compact_session_base}\n\n{checkpoint}"
        changed = False
        for field, value in values.items():
            if agent.get(field) != value:
                agent.set(field, value)
                changed = True
        if changed:
            agent.save(ignore_permissions=True)
    else:
        agent = frappe.get_doc({"doctype": "AI Agent", "agent_name": PURCHASE_AGENT, **values})
        agent.insert(ignore_permissions=True)

    # The old Purchase Invoice workflow is not part of the OCR-to-agent flow.
    # Keeping these grants makes the model load irrelevant invoice skills.
    frappe.db.delete(
        "AI Skill Grant",
        {"agent": PURCHASE_AGENT, "skill": ["in", LEGACY_PURCHASE_INVOICE_SKILLS]},
    )
    for skill in PURCHASE_SKILLS:
        if frappe.db.exists("AI Skill", skill) and not _grant_exists(PURCHASE_AGENT, skill):
            grant = frappe.get_doc({
                "doctype": "AI Skill Grant",
                "agent": PURCHASE_AGENT,
                "skill": skill,
                "grant_type": "All Users",
                "enabled": 1,
            })
            grant.insert(ignore_permissions=True)
            frappe.db.set_value("AI Skill Grant", grant.name, "company", None, update_modified=False)
    frappe.db.commit()


def _grant_exists(agent, skill):
    return bool(frappe.db.sql(
        """
        select name from `tabAI Skill Grant`
        where agent=%s and skill=%s and grant_type='All Users'
          and ifnull(company, '')=''
        limit 1
        """,
        (agent, skill),
    ))


def _assert_ai_enabled():
    if not frappe.db.get_single_value("AI Settings", "enabled"):
        frappe.throw(_("Agent system is disabled."))


def _assert_read(doctype):
    if not frappe.has_permission(doctype, "read", user=frappe.session.user):
        frappe.throw(_("No read permission on {0}.").format(doctype))


def _assert_run_access(run):
    """Read access: the owner, or an auditor when auditing is enabled (logged)."""
    assert_owned_access(run.user, "AI Run", run.name, _("Not authorized."))


def _assert_run_owner(run):
    """Changes, and reuse of a run's content in a new run, are owner-only."""
    if run.user != frappe.session.user:
        frappe.throw(_("Not authorized."), frappe.PermissionError)


def _assert_session_access(session):
    assert_owned_access(session.user, "Financial Attachment Session", session.name, _("Not authorized."))


def _assert_session_owner(session):
    if session.user != frappe.session.user:
        frappe.throw(_("Not authorized."), frappe.PermissionError)


def _assert_company_access(company):
    """Row level: User Permissions may restrict which companies a user can use."""
    if not company or not frappe.has_permission("Company", "read", doc=company, user=frappe.session.user):
        frappe.throw(_("No permission on company {0}.").format(company), frappe.PermissionError)


def _prepare_uploads(files):
    prepared = []
    for item in files:
        filename = (item.get("filename") or "").strip()
        content_base64 = item.get("content_base64") or ""
        if not filename or not content_base64:
            continue
        extension = os.path.splitext(filename)[1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            frappe.throw(_("File type not allowed: {0}").format(extension or filename))
        content = base64.b64decode(content_base64.split(",", 1)[-1], validate=False)
        if len(content) > MAX_FILE_BYTES:
            frappe.throw(_("File is too large: {0}").format(filename))
        prepared.append({
            "filename": filename,
            "extension": extension,
            "content": content,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    if not prepared:
        frappe.throw(_("No valid files were attached."))
    return prepared


def _purchase_retry_key(stage, company, grouping_mode, user_instructions, defaults, prepared_files):
    fingerprint = {
        "version": 2,
        "stage": stage,
        "page": "purchase_invoice_ai",
        "company": company or "",
        "grouping_mode": grouping_mode or "auto",
        "user_instructions": user_instructions or "",
        "defaults": defaults or {},
        "files": sorted([
            {
                "filename": row["filename"],
                "size": row["size"],
                "sha256": row["sha256"],
            }
            for row in prepared_files
        ], key=lambda row: (row["sha256"], row["filename"], row["size"])),
    }
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, default=str)
    return f"purchase_invoice_ai_{stage}:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extraction_message(has_data, reused=False):
    if has_data:
        return _("Reused previous extracted data for the same files and settings.") if reused else _("Extracted journal-entry lines from attachments.")
    return _(
        "لم أستطع استخراج سطور قيد أو أرقام واضحة من المرفقات. قد يكون الملف صورة/مسح ضوئي أو يحتاج OCR بصري أقوى. جرّب رفع PDF نصي أو صورة أوضح، أو أضف تعليمات أكثر تحديدًا ثم أعد الاستخراج."
    )


def _skill_ui_contract():
    contract = {
        "assistant_label": "AI",
        "page_title": _("قراءة المرفقات OCR"),
        "status_collection_label": _("Documents"),
        "primary_collection_label": _("Rows"),
        "primary_collection_keys": ["journal_entry_lines", "entry_lines", "accounting_lines", "lines", "extracted_lines", "observed_lines", "items"],
        "group_collection_keys": ["document_groups", "documents", "invoice_groups", "groups"],
        "stages": [
            {"key": "files", "label": _("الملفات")},
            {"key": "extract", "label": _("قراءة OCR والمراجعة")},
        ],
        "actions": {
            "new": _("عملية جديدة"),
            "extract": _("تجهيز الصفحات"),
            "reextract": _("إعادة تجهيز الصفحات"),
            "open_agent_workspace": _("المعالجة في Agent Workspace"),
        },
        "line_display": [],
    }
    try:
        skill_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "skills",
            FINANCIAL_ATTACHMENTS_SKILL,
            "SKILL.md",
        )
        with open(skill_path, encoding="utf-8") as handle:
            instructions = handle.read()
        match = re.search(r"```json\s+ai_agent_ui_contract\s*(.*?)```", instructions, flags=re.S)
        if not match:
            return contract
        loaded = json.loads(match.group(1))
        if isinstance(loaded, dict):
            contract.update(loaded)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Purchase Invoice AI UI contract load failed")
    return contract


def _save_run_files(run_name, files):
    saved = []
    for item in files:
        filename = item["filename"]
        extension = item["extension"]
        file_doc = save_file(filename, item["content"], "AI Run", run_name, is_private=1)
        saved.append({
            "name": file_doc.name,
            "file_name": file_doc.file_name,
            "file_size": file_doc.file_size,
            "extension": extension,
        })
    if not saved:
        frappe.throw(_("No valid files were attached."))
    return saved


def _create_financial_attachment_session(run, company, grouping_mode, user_instructions, saved_files, pages):
    session = frappe.get_doc({
        "doctype": "Financial Attachment Session",
        "status": "Pages Ready",
        "user": frappe.session.user,
        "company": company,
        "source_run": run.name,
        "grouping_mode": grouping_mode or "auto",
        "user_instructions": user_instructions or "",
        "files": [],
        "pages": [],
    })
    pages_by_file = {}
    for page in pages or []:
        pages_by_file.setdefault(page.get("file_id"), []).append(page)
    for file_info in saved_files or []:
        session.append("files", {
            "file": file_info.get("name"),
            "file_name": file_info.get("file_name"),
            "extension": file_info.get("extension"),
            "file_size": file_info.get("file_size"),
            "page_count": len(pages_by_file.get(file_info.get("name")) or []),
        })
    for page in pages or []:
        session.append("pages", {
            "page_id": page.get("page_id"),
            "file": page.get("file_id"),
            "file_name": page.get("file_name"),
            "page_number": page.get("page"),
            "page_type": page.get("page_type"),
            "status": _session_page_status(page.get("status")),
            "selected": 1 if page.get("can_read") else 0,
            "can_read": 1 if page.get("can_read") else 0,
            "text_preview": page.get("text_preview") or "",
            "warnings_json": json.dumps([page.get("note")] if page.get("note") else [], ensure_ascii=False),
        })
    session.insert(ignore_permissions=True)
    return session


def _session_for_run(run_name):
    name = frappe.db.get_value("Financial Attachment Session", {"source_run": run_name}, "name")
    return frappe.get_doc("Financial Attachment Session", name) if name else None


def _session_payload(session):
    return {
        "name": session.name,
        "status": session.status,
        "company": session.company,
        "source_run": session.source_run,
        "analysis_run": session.analysis_run,
        "grouping_mode": session.grouping_mode,
        "user_instructions": session.user_instructions,
        "current_page_id": session.current_page_id,
        "file_count": session.file_count,
        "page_count": session.page_count,
        "selected_page_count": session.selected_page_count,
        "read_page_count": session.read_page_count,
        "files": [
            {
                "file_id": row.file,
                "file_name": row.file_name,
                "file_url": frappe.db.get_value("File", row.file, "file_url") if row.file else None,
                "extension": row.extension,
                "file_size": row.file_size,
                "page_count": row.page_count,
            }
            for row in session.files
        ],
        "pages": [_session_page_payload(row) for row in session.pages],
    }


def _session_page_payload(row):
    return {
        "page_id": row.page_id,
        "file_id": row.file,
        "file_name": row.file_name,
        "file_url": frappe.db.get_value("File", row.file, "file_url") if row.file else None,
        "page": row.page_number,
        "page_type": row.page_type,
        "status": row.status,
        "selected": 1 if row.selected else 0,
        "can_read": 1 if row.can_read else 0,
        "extraction": row.extraction,
        "text_preview": row.text_preview or "",
        "metadata": _safe_json(row.metadata_json, {}),
        "warnings": _safe_json(row.warnings_json, []),
        "extracted_lines_count": row.extracted_lines_count,
        "analysis_status": row.get("analysis_status") or "",
        "analysis_run": row.get("analysis_run") or "",
        "analysis": _safe_json(row.get("analysis_json"), {}),
    }


def _publish_page_reading_update(run_name, page_item=None):
    """Push the persisted OCR-page state to the workspace immediately."""
    try:
        session = _session_for_run(run_name)
        if not session:
            return

        page = None
        if page_item:
            page_number = cint(page_item.get("page")) or 1
            file_id = page_item.get("file_id")
            page_row = next(
                (
                    row for row in session.pages
                    if row.file == file_id and (cint(row.page_number) or 1) == page_number
                ),
                None,
            )
            if page_row:
                page = _session_page_payload(page_row)

        selected_pages = [row for row in session.pages if row.selected]
        finished_statuses = {"Read", "Failed", "Skipped", "Unsupported"}
        completed = sum(1 for row in selected_pages if row.status in finished_statuses)
        reading = sum(1 for row in selected_pages if row.status == "Reading")
        total = len(selected_pages)
        run = frappe.db.get_value("AI Run", run_name, ["status", "user"], as_dict=True) or {}

        frappe.publish_realtime(
            "ai_agent:page_reading_updated",
            {
                "run_id": run_name,
                "run_status": run.get("status"),
                "session_name": session.name,
                "session_status": session.status,
                "read_page_count": session.read_page_count,
                "progress": {
                    "total": total,
                    "completed": completed,
                    "reading": reading,
                    "percent": round((completed / total * 100) if total else 0, 2),
                },
                "page": page,
            },
            user=run.get("user") or None,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Financial Attachment Page Realtime Update Failed")


def _attachment_reading_from_session(run_name):
    session = _session_for_run(run_name)
    if not session:
        return None
    files = []
    for file_row in session.files:
        pages = []
        for page_row in session.pages:
            if page_row.file != file_row.file:
                continue
            text = str(page_row.text_preview or "").strip()
            pages.append({
                "page_id": page_row.page_id,
                "page": cint(page_row.page_number) or 1,
                "status": str(page_row.status or "").lower(),
                "can_reprocess": 1 if page_row.can_read else 0,
                "extraction": page_row.extraction or "",
                "text_preview": text,
                "metadata": _safe_json(page_row.metadata_json, {}),
                "warnings": _safe_json(page_row.warnings_json, []),
                "extracted_lines_count": cint(page_row.extracted_lines_count),
            })
        if pages:
            files.append({
                "file_id": file_row.file,
                "file_name": file_row.file_name,
                "document_type": "Financial Attachment",
                "role": "source_ocr",
                "pages": pages,
            })
    return {
        "document_groups": [{
            "document_group_id": "GROUP-001",
            "files": files,
            "warnings": [],
            "errors": [],
        }],
        "workflow": {
            "stage": "attachment_reading",
            "source_payload": "financial_attachment_session_saved_ocr",
            "next_step": "model_page_chunk_analysis",
        },
    }


def _safe_json(value, fallback):
    try:
        parsed = json.loads(value or "")
        return parsed if parsed is not None else fallback
    except Exception:
        return fallback


def _session_page_status(status):
    mapping = {
        "ready": "Ready",
        "read": "Read",
        "failed": "Failed",
        "skipped": "Skipped",
        "unsupported": "Unsupported",
    }
    return mapping.get(str(status or "").lower(), "Ready")


def _safe_save_session(session):
    if not session:
        return
    _recount_session_page_totals(session)
    try:
        db_modified = frappe.db.get_value("Financial Attachment Session", session.name, "modified")
        if db_modified:
            session.modified = db_modified
        session.flags.ignore_version = True
        session.flags.ignore_validate = True
        session.flags.ignore_mandatory = True
        session.save(ignore_permissions=True, ignore_version=True)
        frappe.db.commit()
    except Exception:
        try:
            latest = frappe.get_doc("Financial Attachment Session", session.name)
            latest.status = session.status
            _recount_session_page_totals(session)
            latest.read_page_count = getattr(session, "read_page_count", latest.read_page_count)
            latest.selected_page_count = getattr(session, "selected_page_count", latest.selected_page_count)
            latest.page_count = getattr(session, "page_count", latest.page_count)
            latest.file_count = getattr(session, "file_count", latest.file_count)
            page_status_map = {(row.file, cint(row.page_number) or 1): row.status for row in session.pages}
            page_selected_map = {(row.file, cint(row.page_number) or 1): row.selected for row in session.pages}
            for row in latest.pages:
                key = (row.file, cint(row.page_number) or 1)
                if key in page_status_map:
                    row.status = page_status_map[key]
                if key in page_selected_map:
                    row.selected = page_selected_map[key]
            latest.flags.ignore_version = True
            latest.flags.ignore_validate = True
            latest.flags.ignore_mandatory = True
            latest.save(ignore_permissions=True, ignore_version=True)
            frappe.db.commit()
        except Exception:
            pass


def _recount_session_page_totals(session):
    session.page_count = len(session.pages or [])
    session.file_count = len(session.files or [])
    session.selected_page_count = sum(1 for row in (session.pages or []) if row.selected)
    session.read_page_count = sum(1 for row in (session.pages or []) if row.status == "Read")


def _safe_save_run(run):
    if not run:
        return
    try:
        values = {}
        for fieldname in (
            "prompt",
            "status",
            "final_answer",
            "error_summary",
            "started_at",
            "completed_at",
            "cancel_requested",
            "current_step",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
            "estimated_cost",
            "provider_response_id",
            "permission_snapshot_hash",
            "idempotency_key",
        ):
            if hasattr(run, fieldname):
                values[fieldname] = run.get(fieldname)
        if values:
            frappe.db.set_value("AI Run", run.name, values)
        frappe.db.commit()
    except Exception:
        try:
            frappe.db.sql(
                """
                update `tabAI Run`
                set status=%s, final_answer=%s, error_summary=%s,
                    current_step=%s, tool_call_count=%s, completed_at=%s,
                    modified=%s, modified_by=%s
                where name=%s
                """,
                (
                    run.get("status"),
                    run.get("final_answer"),
                    run.get("error_summary"),
                    run.get("current_step"),
                    run.get("tool_call_count"),
                    run.get("completed_at"),
                    frappe.utils.now_datetime(),
                    frappe.session.user,
                    run.name,
                ),
            )
            frappe.db.commit()
        except Exception:
            pass


def _mark_session_pages_selected(session, selected_pages):
    selected_keys = {
        (row.get("file_id"), cint(row.get("page")) or 1)
        for row in selected_pages or []
    }
    for row in session.pages:
        selected = (row.file, cint(row.page_number) or 1) in selected_keys
        row.selected = 1 if selected else 0
        if selected:
            if not (row.status == "Read" and row.text_preview):
                row.status = "Selected"
                row.extraction = None
                row.text_preview = None
                row.metadata_json = None
                row.warnings_json = None
                row.extracted_lines_count = 0
        elif row.status in {"Ready", "Selected", "Reading"}:
            row.status = "Ready"
    session.status = "Reading"
    _safe_save_session(session)


def _session_page_already_read(session, file_id, page_number):
    if not session:
        return False
    for row in session.pages:
        if row.file == file_id and cint(row.page_number) == (cint(page_number) or 1):
            return row.status == "Read" and bool(row.text_preview)
    return False


def _build_workspace_discussion_prompt(company, message, active_step, analysis, source_run_name=None, current_run_name=None):
    compact_ctx = _build_compact_context(company)
    return (
        "Financial attachments workspace chat.\n"
        "You are the actual accounting agent for this page. Reply to the user respectfully, understand the intent, and decide the next UI action.\n"
        "Do not give a canned response. If the user says they will complete accounts inside the Journal Entry, accept that decision and allow opening the draft for review.\n"
        "If they ask you to suggest accounts, decide `suggest_accounts`. If they ask to reprocess/match, decide `run_processing`. If no action is needed, decide `none`.\n"
        "Never insult the user. Keep Arabic clear and practical. Do not expose internal run IDs unless necessary.\n\n"
        "Return compact valid JSON only with this shape:\n"
        "{\n"
        '  "reply_markdown": "Arabic markdown response shown to the user",\n'
        '  "decision": {\n'
        '    "type": "none|suggest_accounts|run_processing|open_journal_review",\n'
        '    "allow_open_with_missing_accounts": false,\n'
        '    "reason": "short reason"\n'
        "  }\n"
        "}\n\n"
        f"Company: {company or ''}\n"
        f"Active step: {active_step or ''}\n"
        f"Source run: {source_run_name or ''}\n"
        f"Current run: {current_run_name or ''}\n"
        f"User message: {message}\n\n"
        f"Pre-loaded Compact Session Context (Leaf Accounts, Cash Suppliers, Cost Centers, Memory, Metadata):\n"
        f"{json.dumps(compact_ctx, ensure_ascii=False)}\n\n"
        "Current analysis summary:\n"
        f"{_discussion_analysis_context(analysis)}"
    )


def _latest_discussion_analysis(payload, current_run_name=None, source_run_name=None):
    for run_name in (current_run_name, source_run_name):
        if not run_name:
            continue
        try:
            run = frappe.get_doc("AI Run", run_name)
            _assert_run_owner(run)
            parsed = _extract_json(run.final_answer or "")
            if not parsed and run.parent_run:
                parsed = _analysis_from_parent_run(run)
            if parsed and run.parent_run and not _analysis_row_count(parsed):
                parsed = _analysis_from_parent_run(run) or parsed
            if parsed and run.parent_run:
                parsed = _postprocess_purchase_analysis(run, parsed)
            if parsed:
                parsed = _canonicalize_journal_payload(parsed)
            if parsed and run.status == "Completed" and run.parent_run:
                parsed = _prepare_journal_payload_for_review(parsed, run.company)
            if _analysis_row_count(parsed):
                return parsed
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Financial Attachment Discussion Analysis Refresh Failed")
    return payload if isinstance(payload, dict) else {}


def _discussion_analysis_context(analysis):
    """Dynamically present full analysis context to PURCHASE_AGENT without rigid hardcoded structures."""
    if not isinstance(analysis, dict):
        return "{}"
    return json.dumps(analysis, ensure_ascii=False, default=str)


def _cash_supplier_mobile_field(meta):
    """Find mobile/phone field dynamically from Cash Supplier metadata."""
    for df in meta.fields:
        if df.fieldtype == "Phone" or any(k in df.fieldname.lower() for k in ("mobile", "phone", "contact", "cell", "tel")):
            return df.fieldname
    return None


def _cash_supplier_tax_field(meta):
    """Find tax ID field dynamically from Cash Supplier metadata."""
    for df in meta.fields:
        if any(k in df.fieldname.lower() for k in ("tax", "vat", "trn")):
            return df.fieldname
    return None


def _postprocess_purchase_analysis(run, payload):
    """Keep model output intact; no local reconstruction from detected OCR rows."""
    return _normalize_analysis_payload(payload)


def _attachment_reading_from_run(run, recovery_warning=True):
    detected_files = _detected_purchase_files(run.name)
    if not detected_files:
        return None
    mode = _prompt_grouping_mode(run.prompt)
    warning = _("تمت قراءة المرفقات من الملفات مباشرة بعد تعذر الحصول على JSON من النموذج.") if recovery_warning else None
    if mode == "separate":
        groups = [
            _document_group_from_attachment_files([file_info], f"GROUP-{index:03d}", warning=warning)
            for index, file_info in enumerate(detected_files, start=1)
        ]
    else:
        groups = [_document_group_from_attachment_files(detected_files, "GROUP-001", warning=warning)]
    return {
        "document_groups": groups,
        "workflow": {
            "stage": "attachment_reading",
            "next_step": "agent_skill_processing_to_journal_entry",
        },
    }


def _document_group_from_attachment_files(detected_files, group_id, warning=None):
    extracted_lines = []
    files = []
    for file_info in detected_files:
        pages = []
        for page in file_info.get("pages") or []:
            page_rows = []
            for row in page.get("rows") or []:
                line = _detected_row_to_extracted_line(row, file_info)
                line["line_no"] = len(extracted_lines) + 1
                extracted_lines.append(line)
                page_rows.append(line)
            pages.append({
                "page_id": f"{file_info.get('file_id')}::p{page.get('page') or 1}",
                "page": page.get("page"),
                "status": page.get("status") or ("read" if page.get("text_preview") else "failed"),
                "can_reprocess": page.get("can_reprocess", 1),
                "extraction": page.get("extraction"),
                "text_preview": page.get("text_preview") or "",
                "extracted_lines_count": len(page_rows),
                "warnings": page.get("warnings") or [],
                "metadata": _generic_metadata(page.get("metadata") or {}),
            })
        files.append({
            "file_id": file_info.get("file_id"),
            "file_name": file_info.get("file_name"),
            "pages": pages,
        })

    _apply_group_values_to_extracted_lines(extracted_lines, detected_files)
    return {
        "document_group_id": group_id,
        "files": files,
        "extracted_lines": extracted_lines,
        "warnings": [warning] if warning else [],
    }


def _replace_page_in_attachment_reading(analysis, file_doc, page_result):
    groups = _analysis_groups(analysis)
    if not groups:
        groups = [{"document_group_id": "GROUP-001", "files": [], "extracted_lines": [], "warnings": []}]
        if isinstance(analysis, dict):
            analysis["document_groups"] = groups

    group = groups[0]
    files = group.setdefault("files", [])
    file_info = None
    for candidate in files:
        if candidate.get("file_id") == file_doc.name or candidate.get("file_name") == file_doc.file_name:
            file_info = candidate
            break
    if not file_info:
        file_info = {
            "file_id": file_doc.name,
            "file_name": file_doc.file_name or file_doc.name,
            "pages": [],
        }
        files.append(file_info)

    page_number = page_result.get("page") or 1
    normalized_page = {
        "page_id": f"{file_doc.name}::p{page_number}",
        "page": page_number,
        "status": page_result.get("status") or ("read" if page_result.get("text_preview") else "failed"),
        "can_reprocess": page_result.get("can_reprocess", 1),
        "extraction": page_result.get("extraction"),
        "text_preview": page_result.get("text_preview") or "",
        "extracted_lines_count": cint(page_result.get("extracted_lines_count")) or len(page_result.get("rows") or []),
        "rows": page_result.get("rows") or [],
        "totals": page_result.get("totals") or {},
        "warnings": page_result.get("warnings") or [],
        "metadata": _generic_metadata(page_result.get("metadata") or {}),
    }

    replaced = False
    pages = file_info.setdefault("pages", [])
    for index, current in enumerate(pages):
        if cint(current.get("page")) == cint(page_number):
            pages[index] = normalized_page
            replaced = True
            break
    if not replaced:
        pages.append(normalized_page)
        pages.sort(key=lambda row: cint(row.get("page")) or 0)

    existing_lines = [
        line for line in (group.get("extracted_lines") or [])
        if not (
            line.get("source_file") == (file_doc.file_name or file_doc.name)
            and cint(line.get("source_page")) == cint(page_number)
        )
    ]
    page_lines = []
    file_info_for_line = {"file_name": file_doc.file_name or file_doc.name}
    for row in page_result.get("rows") or []:
        line = _detected_row_to_extracted_line(row, file_info_for_line)
        line["source_page"] = page_number
        page_lines.append(line)
    group["extracted_lines"] = existing_lines + page_lines
    for index, line in enumerate(group["extracted_lines"], start=1):
        line["line_no"] = index


def _generic_metadata(metadata):
    return {
        key: value
        for key, value in {
            "tax_identifier": metadata.get("tax_identifier") or metadata.get("custom_tax_id"),
            "document_date": metadata.get("document_date") or metadata.get("custom_bill_date"),
            "document_number": metadata.get("document_number") or metadata.get("custom_bill_no"),
            "document_amount": metadata.get("document_amount"),
        }.items()
        if value
    }


def _detected_row_to_extracted_line(row, file_info):
    return {
        "description": row.get("description") or row.get("raw_line") or "",
        "quantity": flt(row.get("quantity") or row.get("qty")),
        "unit": row.get("unit") or row.get("uom") or "",
        "unit_price": flt(row.get("unit_price") or row.get("rate")),
        "amount": flt(row.get("amount") or row.get("line_total") or row.get("net_amount")),
        "tax_amount": flt(row.get("tax_amount") or row.get("custom_tax_amount")),
        "tax_identifier": row.get("tax_identifier") or row.get("custom_tax_id") or "",
        "document_number": row.get("document_number") or row.get("custom_bill_no") or "",
        "document_date": row.get("document_date") or row.get("custom_bill_date") or "",
        "source_file": file_info.get("file_name"),
        "source_page": cint(row.get("source_page")) or 1,
    }


def _apply_group_values_to_extracted_lines(lines, detected_files):
    if not lines:
        return
    tax_identifier = ""
    document_date = ""
    page_values = {}
    tax_total = 0
    amount_total = sum(flt(row.get("amount")) for row in lines)
    for file_info in detected_files:
        tax_total += flt((file_info.get("totals") or {}).get("tax_amount"))
        for page in file_info.get("pages") or []:
            metadata = _generic_metadata(page.get("metadata") or {})
            page_key = (file_info.get("file_name"), cint(page.get("page")) or 1)
            page_values[page_key] = {
                "tax_identifier": metadata.get("tax_identifier") or "",
                "document_date": metadata.get("document_date") or "",
                "document_number": _valid_document_number(
                    metadata.get("document_number"),
                    metadata.get("tax_identifier"),
                ),
                "supplier": metadata.get("supplier") or "",
            }
            tax_identifier = tax_identifier or metadata.get("tax_identifier") or ""
            document_date = document_date or metadata.get("document_date") or ""
    distributed = 0
    for index, line in enumerate(lines):
        page_meta = page_values.get((line.get("source_file"), cint(line.get("source_page")) or 1)) or {}
        page_tax_identifier = page_meta.get("tax_identifier") or tax_identifier
        page_document_date = page_meta.get("document_date") or document_date
        page_document_number = page_meta.get("document_number") or ""
        page_supplier = page_meta.get("supplier") or ""
        if page_tax_identifier and not line.get("tax_identifier"):
            line["tax_identifier"] = page_tax_identifier
        if page_document_date and not line.get("document_date"):
            line["document_date"] = page_document_date
        if page_document_number and not _is_plausible_document_number(line.get("document_number")):
            line["document_number"] = page_document_number
        if page_supplier and not line.get("supplier"):
            line["supplier"] = page_supplier
            line["custom_cash_supplier"] = page_supplier
        if tax_total and not flt(line.get("tax_amount")):
            if index == len(lines) - 1:
                tax_value = round(tax_total - distributed, 2)
            else:
                tax_value = round(tax_total * flt(line.get("amount")) / amount_total, 2) if amount_total else round(tax_total / len(lines), 2)
                distributed += tax_value
            line["tax_amount"] = tax_value


def _analysis_from_parent_run(run):
    if not run.parent_run:
        return None
    try:
        parent = frappe.get_doc("AI Run", run.parent_run)
    except Exception:
        return None
    parsed = _extract_json(parent.final_answer or "")
    if not parsed:
        return None
    return parsed


def _prompt_grouping_mode(prompt):
    match = re.search(r"^Grouping mode:\s*(\w+)", prompt or "", re.M)
    return (match.group(1) if match else "auto").strip().lower()


def _detected_purchase_files(run_name):
    service = FileContextService()
    detected = []
    for file_doc in service.get_run_files(run_name)[:MAX_FILES]:
        extension = os.path.splitext(file_doc.file_name or file_doc.name)[1].lower()
        if extension == ".pdf":
            page_results = service.extract_pdf_invoice_pages(file_doc)
        elif extension in {".png", ".jpg", ".jpeg", ".webp"}:
            page_results = service.extract_image_attachment_page(file_doc)
        elif extension in TEXT_DOCUMENT_EXTENSIONS:
            page_results = [service.extract_text_attachment_page(file_doc)]
        else:
            page_results = []
        rows = []
        totals = {}
        if page_results:
            for page in page_results:
                for row in page.get("rows") or []:
                    merged_row = dict(row)
                    merged_row["source_page"] = page.get("page")
                    for key, value in (page.get("metadata") or {}).items():
                        if value and not merged_row.get(key):
                            merged_row[key] = value
                    rows.append(merged_row)
                for key, value in (page.get("totals") or {}).items():
                    totals[key] = round(flt(totals.get(key)) + flt(value), 2)
        else:
            summary = service.summarize_file(file_doc)
            rows = _decode_context_json(summary, "- detected_attachment_table_rows:")
            totals = _decode_context_json(summary, "- detected_attachment_totals:")
        if rows or page_results:
            detected.append({
                "file_id": file_doc.name,
                "file_name": file_doc.file_name or file_doc.name,
                "rows": rows if isinstance(rows, list) else [],
                "totals": totals if isinstance(totals, dict) else {},
                "pages": page_results,
            })
    return detected


def _decode_context_json(text, marker):
    index = (text or "").find(marker)
    if index < 0:
        return None
    chunk = text[index + len(marker):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(chunk)
        return value
    except Exception:
        return None


def _run_files(run_name):
    return frappe.get_all(
        "File",
        filters={"attached_to_doctype": "AI Run", "attached_to_name": run_name, "is_folder": 0},
        fields=["name", "file_name", "file_size", "file_url"],
        order_by="creation asc",
        page_length=MAX_FILES,
    )


def _run_steps(run_name):
    return frappe.get_all(
        "AI Run Step",
        filters={"run": run_name, "is_user_visible": 1},
        fields=["sequence", "step_type", "title", "summary", "status", "duration_ms"],
        order_by="sequence asc",
        page_length=80,
    )


def _attachment_page_manifest(run_name):
    service = FileContextService()
    pages = []
    for file_doc in service.get_run_files(run_name)[:MAX_FILES]:
        for page in service.render_attachment_pages(file_doc):
            pages.append(page)
    return pages


def _manifest_files_for_storage(pages):
    files = {}
    for page in pages or []:
        file_id = page.get("file_id")
        if not file_id:
            continue
        current = files.setdefault(file_id, {
            "file_id": file_id,
            "file_name": page.get("file_name"),
            "pages": [],
        })
        current["pages"].append({
            "page_id": page.get("page_id"),
            "page": page.get("page"),
            "page_type": page.get("page_type"),
            "status": page.get("status"),
            "can_reprocess": 1 if page.get("can_read") else 0,
            "text_preview": page.get("text_preview") or "",
            "warnings": [page.get("note")] if page.get("note") else [],
            "metadata": {},
        })
    return list(files.values())


def _start_background_page_reading(run_name, selected_pages, user, site):
    """Queue one sequential Frappe job; each page uses its own OCR subprocess.

    The session/page manifest already exists. This function does not split PDFs
    or create page rows. It only processes the selected existing session pages
    in order, preventing simultaneous high-memory OCR processes.
    """
    if not selected_pages:
        return
    queue_name = str(frappe.conf.get("ai_agent_ocr_queue") or "long")
    timeout = cint(frappe.conf.get("ai_agent_ocr_batch_timeout") or 7200)
    try:
        frappe.enqueue(
            "ai_agent.purchase_invoice_ai.api.execute_sequential_page_reading_job",
            queue=queue_name,
            timeout=timeout,
            job_id=run_job_id(run_name),
            deduplicate=True,
            run_name=run_name,
            selected_pages=selected_pages,
            user=user,
            enqueue_after_commit=True,
        )
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Financial Attachment Sequential Page Reading Enqueue Failed",
        )
        mark_interrupted_run(
            run_name,
            _("Financial attachment reading could not be added to the background queue. Please retry."),
        )


def execute_sequential_page_reading_job(run_name, selected_pages, user=None):
    """Process existing Financial Attachment Session pages one by one."""
    # Never fall back to Administrator: page reading runs as the run owner.
    job_user = user or frappe.db.get_value("AI Run", run_name, "user")
    if not job_user or job_user == "Guest" or not frappe.db.exists("User", {"name": job_user, "enabled": 1}):
        frappe.log_error(
            f"AI Run {run_name} has no valid owner user; page reading refused.",
            "Financial Attachment Page Reading Rejected",
        )
        mark_interrupted_run(run_name, _("Page reading was rejected because the run has no owner user."))
        return
    frappe.set_user(job_user)
    ordered_pages = sorted(
        selected_pages or [],
        key=lambda row: (str(row.get("file_id") or ""), cint(row.get("page")) or 1),
    )
    for page_item in ordered_pages:
        if _should_stop_page_reading(run_name):
            _cancel_page_reading_session(run_name)
            return
        page_result = _complete_single_page_reading(run_name, page_item)
        # Persist the page result before starting the next isolated OCR process.
        frappe.db.commit()
        fatal_error = _fatal_page_ocr_error(page_result)
        if fatal_error:
            _fail_page_reading_batch(run_name, fatal_error)
            return
        if _should_stop_page_reading(run_name):
            _cancel_page_reading_session(run_name)
            return
        pause = float(frappe.conf.get("ai_agent_ocr_page_pause_seconds") or 0.25)
        if pause > 0:
            time.sleep(min(pause, 5))
    _finalize_page_reading_if_done(run_name)


def _complete_single_page_reading(run_name, page_item):
    run = frappe.get_doc("AI Run", run_name)
    if run.status in TERMINAL_STATUSES or run.cancel_requested:
        _cancel_page_reading_session(run_name)
        return
    run.status = "Running"
    run.current_step = 1
    _safe_save_run(run)
    _mark_single_page_status(run_name, page_item, "Reading")
    TraceService().publish_realtime("ai_agent:run_updated", run_name)

    file_doc = _page_file_doc(run, page_item)
    if not file_doc:
        _mark_single_page_failed(run_name, page_item, _("لم يتم العثور على ملف الصفحة."))
        return

    page_number = cint(page_item.get("page")) or 1
    trace = TraceService()
    sequence = frappe.db.count("AI Run Step", {"run": run_name}) + 1
    step = trace.add_step(
        run_name,
        sequence,
        "Tool",
        _("قراءة صفحة {0}").format(page_number),
        _("الملف: {0}").format(file_doc.file_name or file_doc.name),
    )
    try:
        service = FileContextService()
        page_result = _extract_page_result(service, file_doc, page_number, run_name=run_name)
        if page_result is None:
            _mark_single_page_failed(run_name, page_item, _("نوع الملف غير مدعوم للقراءة الصفحية."))
            trace.complete_step(step.name, status="Failed", summary=_("نوع الملف غير مدعوم للقراءة الصفحية."))
            return

        page_result["page"] = page_number
        _merge_single_page_result(run_name, file_doc, page_result)
        trace.complete_step(
            step.name,
            summary=_("تمت قراءة الصفحة. العناصر النصية المستخرجة: {0}").format(cint(page_result.get("extracted_lines_count")) or len(page_result.get("rows") or [])),
        )
        return page_result
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "Financial Attachment Single Page Read Failed")
        _mark_single_page_failed(run_name, page_item, _("تعذرت قراءة هذه الصفحة: {0}").format(str(exc)))
        try:
            trace.complete_step(step.name, status="Failed", summary=_("تعذرت قراءة هذه الصفحة."))
        except Exception:
            pass
    return None


def _fatal_page_ocr_error(page_result):
    if not isinstance(page_result, dict):
        return ""
    message = "\n".join(str(item or "") for item in page_result.get("warnings") or [])
    if not message:
        return ""
    from ai_agent.runtime.gemini_ocr import is_fatal_gemini_ocr_error

    return message if is_fatal_gemini_ocr_error(message) else ""


def _fail_page_reading_batch(run_name, message):
    message = str(message or _("تعذر محرك OCR.")).strip()[:1000]
    session = _session_for_run(run_name)
    if session:
        for row in session.pages:
            if row.selected and row.status != "Read":
                frappe.db.set_value(
                    row.doctype,
                    row.name,
                    {
                        "status": "Failed",
                        "warnings_json": json.dumps([message], ensure_ascii=False),
                    },
                    update_modified=False,
                )
        session.reload()
        frappe.db.set_value(
            "Financial Attachment Session",
            session.name,
            {
                "status": "Failed",
                "read_page_count": sum(1 for row in session.pages if row.status == "Read"),
                "selected_page_count": sum(1 for row in session.pages if row.selected),
            },
            update_modified=False,
        )
    if frappe.db.exists("AI Run", run_name):
        frappe.db.set_value(
            "AI Run",
            run_name,
            {
                "status": "Failed",
                "error_summary": message,
                "completed_at": frappe.utils.now_datetime(),
            },
            update_modified=False,
        )
    frappe.db.commit()
    _publish_page_reading_update(run_name)
    TraceService().publish_realtime("ai_agent:run_updated", run_name)


def _should_stop_page_reading(run_name):
    values = frappe.db.get_value("AI Run", run_name, ["status", "cancel_requested"], as_dict=True)
    if not values:
        return True
    return bool(values.get("cancel_requested") or values.get("status") in {"Cancelled", "Timed Out"})


def _cancel_page_reading_session(run_name):
    session = _session_for_run(run_name)
    if session:
        for row in session.pages:
            if row.status in {"Selected", "Reading"}:
                frappe.db.set_value(
                    row.doctype,
                    row.name,
                    {
                        "status": "Skipped",
                        "warnings_json": json.dumps([_("تم إيقاف قراءة هذه الصفحة قبل تشغيل OCR.")], ensure_ascii=False),
                    },
                    update_modified=False,
                )
        session.reload()
        _refresh_session_reading_status(session)
    if frappe.db.exists("AI Run", run_name):
        frappe.db.set_value(
            "AI Run",
            run_name,
            {
                "status": "Cancelled",
                "cancel_requested": 1,
                "error_summary": _("تم إيقاف قراءة الصفحات."),
                "completed_at": frappe.utils.now_datetime(),
            },
            update_modified=False,
        )
    frappe.db.commit()
    _publish_page_reading_update(run_name)
    TraceService().publish_realtime("ai_agent:run_updated", run_name)


def _configured_ocr_engine():
    """Return the OCR engine selected in the singleton AI Settings."""
    return str(
        frappe.db.get_single_value("Gemini OCR Settings", "ocr_engine") or ""
    ).strip()


def _uses_local_ocr_engine():
    """Use the isolated local OCR bridge only for the explicit local option."""
    return _configured_ocr_engine() == LOCAL_OCR_ENGINE


def _extract_page_result(service, file_doc, page_number, run_name=None):
    """Read one existing Financial Attachment Session page.

    When AI Settings.ocr_engine is ``Local Engine (PaddleOCR / PyMuPDF)``, PDF
    and image pages are routed lazily to ``ai_agent.runtime.local_ocr``. Other
    OCR engines keep using FileContextService. PDF splitting and session-page
    creation remain unchanged and happen before this function is called.
    """
    extension = os.path.splitext(file_doc.file_name or file_doc.name)[1].lower()

    if extension in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
        if _uses_local_ocr_engine():
            # Lazy import is intentional: do not load the local OCR bridge when
            # another OCR engine is selected.
            from ai_agent.runtime.local_ocr import extract_financial_attachment_page

            return extract_financial_attachment_page(file_doc, page_number, run_name=run_name or "")

        if extension == ".pdf":
            return service.extract_pdf_attachment_page(file_doc, page_number)

        page_result = (service.extract_image_attachment_page(file_doc) or [{}])[0]
        page_result["page"] = 1
        return page_result

    if extension in TEXT_DOCUMENT_EXTENSIONS:
        page_result = service.extract_text_attachment_page(file_doc)
        page_result["page"] = 1
        return page_result

    return None


def _page_file_doc(run, page_item):
    file_id = page_item.get("file_id")
    if not file_id:
        return None
    try:
        if frappe.db.exists("File", file_id):
            return frappe.get_doc("File", file_id)
        found = frappe.db.get_value("File", {"file_name": file_id}, "name")
        if found:
            return frappe.get_doc("File", found)
    except Exception:
        pass
    try:
        run_files = FileContextService().get_run_files(run.name)[:MAX_FILES]
        for row in run_files:
            if row.name == file_id or row.file_name == file_id:
                return row
        if run_files:
            return run_files[0]
    except Exception:
        pass
    return None


def _mark_single_page_failed(run_name, page_item, message):
    session = _session_for_run(run_name)
    if session:
        page_number = cint(page_item.get("page")) or 1
        row_name = frappe.db.get_value(
            "Financial Attachment Session Page",
            {
                "parent": session.name,
                "parenttype": "Financial Attachment Session",
                "parentfield": "pages",
                "file": page_item.get("file_id"),
                "page_number": page_number,
            },
            "name",
        )
        if row_name:
            frappe.db.set_value(
                "Financial Attachment Session Page",
                row_name,
                {
                    "status": "Failed",
                    "warnings_json": json.dumps([message], ensure_ascii=False),
                },
                update_modified=False,
            )
            frappe.db.commit()
        session.reload()
        _refresh_session_reading_status(session)
    _finalize_page_reading_if_done(run_name)
    _publish_page_reading_update(run_name, page_item)


def _mark_single_page_status(run_name, page_item, status):
    session = _session_for_run(run_name)
    if not session:
        return
    row_name = frappe.db.get_value(
        "Financial Attachment Session Page",
        {
            "parent": session.name,
            "parenttype": "Financial Attachment Session",
            "parentfield": "pages",
            "file": page_item.get("file_id"),
            "page_number": cint(page_item.get("page")) or 1,
        },
        "name",
    )
    if row_name:
        frappe.db.set_value(
            "Financial Attachment Session Page",
            row_name,
            "status",
            status,
            update_modified=False,
        )
        frappe.db.set_value(
            "Financial Attachment Session",
            session.name,
            "status",
            "Reading",
            update_modified=False,
        )
        frappe.db.commit()
        _publish_page_reading_update(run_name, page_item)


def _merge_single_page_result(run_name, file_doc, page_result):
    lock_name = f"financial_attachment_page_read:{run_name}"
    frappe.db.sql("SELECT GET_LOCK(%s, 30)", lock_name)
    is_done = False
    try:
        run = frappe.get_doc("AI Run", run_name)
        analysis = _extract_json(run.final_answer or "") or _attachment_reading_from_run(run, recovery_warning=False) or {"document_groups": []}
        _replace_page_in_attachment_reading(analysis, file_doc, page_result)
        _update_session_single_page(run_name, file_doc, page_result)
        session = _session_for_run(run_name)
        is_done = _session_reading_is_done(session)
        run.reload()
        if run.cancel_requested or run.status in {"Cancelled", "Timed Out"}:
            run.status = "Cancelled"
            run.error_summary = _("تم إيقاف قراءة الصفحات.")
            run.completed_at = frappe.utils.now_datetime()
            _safe_save_run(run)
            return
        run.status = "Completed" if is_done else "Running"
        run.final_answer = json.dumps(analysis, ensure_ascii=False, default=str)
        run.current_step = 1
        run.tool_call_count = 0
        if is_done:
            run.completed_at = frappe.utils.now_datetime()
        _safe_save_run(run)
    finally:
        frappe.db.sql("SELECT RELEASE_LOCK(%s)", lock_name)
    if not is_done:
        _finalize_page_reading_if_done(run_name)
    _publish_page_reading_update(
        run_name,
        {"file_id": file_doc.name, "page": page_result.get("page")},
    )
    TraceService().publish_realtime("ai_agent:run_updated", run_name)


def _update_session_single_page(run_name, file_doc, page_result):
    session = _session_for_run(run_name)
    if not session:
        return
    page_number = cint(page_result.get("page")) or 1
    for row in session.pages:
        if row.file == file_doc.name and cint(row.page_number) == page_number:
            frappe.db.set_value(
                row.doctype,
                row.name,
                {
                    "status": "Read" if page_result.get("text_preview") else "Failed",
                    "extraction": page_result.get("extraction"),
                    "text_preview": page_result.get("text_preview") or "",
                    "metadata_json": json.dumps(page_result.get("metadata") or {}, ensure_ascii=False, default=str),
                    "warnings_json": json.dumps(page_result.get("warnings") or [], ensure_ascii=False, default=str),
                    "extracted_lines_count": cint(page_result.get("extracted_lines_count")) or len(page_result.get("rows") or []),
                },
                update_modified=False,
            )
            break
    session.reload()
    _refresh_session_reading_status(session)


def _refresh_session_reading_status(session):
    if not session:
        return
    changed_rows = False
    for row in session.pages:
        if row.status in {"Reading", "Selected"} and row.text_preview:
            frappe.db.set_value(row.doctype, row.name, "status", "Read", update_modified=False)
            row.status = "Read"
            changed_rows = True

    session.read_page_count = sum(1 for row in session.pages if row.status == "Read")
    session.selected_page_count = sum(1 for row in session.pages if row.selected)
    selected = [row for row in session.pages if row.selected]
    pending = [row for row in selected if row.status in {"Reading", "Selected"}]
    session.status = "Reading" if pending else "Read"
    if changed_rows:
        session.reload()
        session.read_page_count = sum(1 for row in session.pages if row.status == "Read")
        session.selected_page_count = sum(1 for row in session.pages if row.selected)
        selected = [row for row in session.pages if row.selected]
        pending = [row for row in selected if row.status in {"Reading", "Selected"}]
        session.status = "Reading" if pending else "Read"
    frappe.db.set_value(
        "Financial Attachment Session",
        session.name,
        {
            "status": session.status,
            "read_page_count": session.read_page_count,
            "selected_page_count": session.selected_page_count,
        },
        update_modified=False,
    )
    frappe.db.commit()


def _session_reading_is_done(session):
    if not session:
        return False
    selected = [row for row in session.pages if row.selected]
    if not selected:
        return False
    return not any(row.status in {"Reading", "Selected"} for row in selected)


def _finalize_page_reading_if_done(run_name):
    session = _session_for_run(run_name)
    if session:
        _refresh_session_reading_status(session)
    if session and not _session_reading_is_done(session):
        return
    run = frappe.get_doc("AI Run", run_name)
    if run.cancel_requested or run.status in {"Cancelled", "Timed Out"}:
        run.status = "Cancelled"
        run.error_summary = _("تم إيقاف قراءة الصفحات.")
        run.completed_at = frappe.utils.now_datetime()
        _safe_save_run(run)
        _publish_page_reading_update(run_name)
        TraceService().publish_realtime("ai_agent:run_updated", run_name)
        return
    selected = [row for row in session.pages if row.selected] if session else []
    # Judge success against the WHOLE session, not just the currently selected
    # rows: re-reading one failed page must never bury pages that were already
    # read successfully in an earlier pass (a run marked Failed here hid 13
    # readable pages in FA-SESSION-1387).
    read_any = any(row.status == "Read" for row in session.pages) if session else False
    if selected and not read_any:
        message = _("لم تتم قراءة أي صفحة محددة بنجاح. راجع أخطاء محرك OCR ثم أعد المحاولة.")
        run.status = "Failed"
        run.error_summary = message
        run.completed_at = frappe.utils.now_datetime()
        _safe_save_run(run)
        frappe.db.set_value("Financial Attachment Session", session.name, "status", "Failed", update_modified=False)
        frappe.db.commit()
        _publish_page_reading_update(run_name)
        TraceService().publish_realtime("ai_agent:run_updated", run_name)
        return
    if session and session.status == "Failed":
        # An earlier partial failure marked the session Failed; readable pages
        # exist now, so let the recount reflect them instead of the dead state.
        _refresh_session_reading_status(session)
    raw_ocr = _extract_json(run.final_answer or "") or _attachment_reading_from_run(run, recovery_warning=False) or {"document_groups": []}
    run.status = "Completed"
    run.final_answer = json.dumps(raw_ocr, ensure_ascii=False, default=str)
    run.current_step = 1
    run.tool_call_count = 0
    run.completed_at = frappe.utils.now_datetime()
    _safe_save_run(run)
    _publish_page_reading_update(run_name)
    TraceService().publish_realtime("ai_agent:run_completed", run_name)


def _start_background_run(run_name, user, site, enqueue_after_commit=True):
    try:
        frappe.enqueue(
            "ai_agent.purchase_invoice_ai.api.execute_run_job",
            queue="long",
            timeout=1800,
            job_id=run_job_id(run_name),
            deduplicate=True,
            run_name=run_name,
            user=user,
            enqueue_after_commit=enqueue_after_commit,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Purchase Invoice AI Run Enqueue Failed")
        mark_interrupted_run(
            run_name,
            _("Purchase invoice analysis could not be added to the background queue. Please retry."),
        )


def execute_run_job(run_name, user=None):
    try:
        run = frappe.get_doc("AI Run", run_name)
        if run.status in TERMINAL_STATUSES or run.cancel_requested:
            return
        # Never fall back to Administrator: that would bypass the entire
        # permission-intersection model the platform is built on.
        job_user = user or run.user
        if not job_user or job_user == "Guest":
            frappe.log_error(
                f"AI Run {run_name} has no valid owner user; execution refused.",
                "Purchase Invoice AI Run Rejected",
            )
            mark_interrupted_run(run_name, _("Analysis was rejected because the run has no owner user."))
            return
        frappe.set_user(job_user)
        execute_run(run_name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Purchase Invoice AI Run Failed")
        try:
            run = frappe.get_doc("AI Run", run_name)
            if run.status not in TERMINAL_STATUSES:
                run.status = "Failed"
                run.error_summary = _("Purchase invoice analysis failed. Check Error Log.")
                run.completed_at = frappe.utils.now_datetime()
                _safe_save_run(run)
            TraceService().publish_realtime("ai_agent:run_failed", run_name)
        except Exception:
            pass
