import base64
import hashlib
import json
import os
import re
import time

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate
from frappe.utils.file_manager import get_file_path, save_file

from ai_agent.runtime.file_context import FileContextService, TEXT_DOCUMENT_EXTENSIONS
from ai_agent.runtime.orchestrator import execute_run
from ai_agent.runtime.skill_registry import SkillRegistry
from ai_agent.runtime.trace_service import TraceService


PURCHASE_AGENT = "Purchase Invoice AI"
FINANCIAL_ATTACHMENTS_SKILL = "financial-attachments-to-journal-entry"
PURCHASE_SKILLS = [
    FINANCIAL_ATTACHMENTS_SKILL,
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
MAX_ANALYSIS_ROW_TEXT_CHARS = 2400
MAX_ANALYSIS_PAGE_CHUNK_CHARS = 18000
MAX_ANALYSIS_PAGES_PER_CHUNK = 1
MAX_MODEL_PAGE_TEXT_CHARS = 6000
MAX_MODEL_CONTEXT_ACCOUNTS = 40
MAX_MODEL_CONTEXT_SUPPLIERS = 30
MAX_MODEL_CONTEXT_COST_CENTERS = 20
MAX_MODEL_CONTEXT_MEMORIES = 8
BACKGROUND_MODEL_MATCHING_CONF = "purchase_invoice_ai_background_model_matching"
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
        "companies": frappe.get_all("Company", fields=["name", "default_currency"], order_by="name asc"),
        "today": nowdate(),
        "max_files": MAX_FILES,
        "max_file_mb": int(MAX_FILE_BYTES / (1024 * 1024)),
        "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        "ui_contract": _skill_ui_contract(),
    }


@frappe.whitelist()
def clean_internal_ocr_warnings():
    if "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized."))
    replacement = _("تعذر محرك OCR الاحتياطي. يمكن إعادة قراءة الصفحة لاحقًا أو مراجعة إعدادات OCR.")
    patterns = [
        re.compile(r"Gemini OCR:\s*cannot import name 'run_local_ocr_bytes'[^\"\\\]]*", re.I),
        re.compile(r"Gemini OCR:\s*[^\"\\\]]*/home/snd/frappe-bench[^\"\\\]]*", re.I),
        re.compile(r"Gemini OCR is disabled in Gemini OCR Settings\.", re.I),
    ]

    def clean_text(value):
        text = str(value or "")
        changed = False
        for pattern in patterns:
            text, count = pattern.subn(replacement, text)
            changed = changed or bool(count)
        return text, changed

    cleaned_runs = 0
    for row in frappe.db.sql(
        """
        select name, final_answer from `tabAI Run`
        where final_answer like %s
           or final_answer like %s
           or final_answer like %s
        """,
        ("%Gemini OCR:%", "%run_local_ocr_bytes%", "%Gemini OCR is disabled in Gemini OCR Settings%"),
        as_dict=True,
    ):
        cleaned, changed = clean_text(row.final_answer)
        if changed:
            frappe.db.set_value("AI Run", row.name, "final_answer", cleaned, update_modified=False)
            cleaned_runs += 1

    cleaned_pages = 0
    for row in frappe.db.sql(
        """
        select name, warnings_json from `tabFinancial Attachment Session Page`
        where warnings_json like %s
           or warnings_json like %s
           or warnings_json like %s
        """,
        ("%Gemini OCR:%", "%run_local_ocr_bytes%", "%Gemini OCR is disabled in Gemini OCR Settings%"),
        as_dict=True,
    ):
        cleaned, changed = clean_text(row.warnings_json)
        if not changed:
            continue
        try:
            parsed = json.loads(cleaned or "[]")
            if isinstance(parsed, list):
                deduped = []
                for item in parsed:
                    if item and item not in deduped:
                        deduped.append(item)
                cleaned = json.dumps(deduped, ensure_ascii=False)
        except Exception:
            pass
        frappe.db.set_value("Financial Attachment Session Page", row.name, "warnings_json", cleaned, update_modified=False)
        cleaned_pages += 1

    frappe.db.commit()
    return {"cleaned_runs": cleaned_runs, "cleaned_session_pages": cleaned_pages}


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


@frappe.whitelist()
def start_analysis_from_review(source_run_name, analysis, user_instructions="", defaults=None):
    from ai_agent.purchase_invoice_ai.journal_analysis import start_analysis_from_review as service

    return service(source_run_name, analysis, user_instructions=user_instructions, defaults=defaults)


@frappe.whitelist()
def reanalyze_from_saved_ocr(run_name, user_instructions="", defaults=None):
    from ai_agent.purchase_invoice_ai.journal_analysis import reanalyze_from_saved_ocr as service

    return service(run_name, user_instructions=user_instructions, defaults=defaults)


@frappe.whitelist()
def process_next_analysis_chunk(run_name):
    from ai_agent.purchase_invoice_ai.journal_analysis import process_next_analysis_chunk as service

    return service(run_name)


def _prefer_latest_source_reading(source_run, reviewed):
    latest = _attachment_reading_from_session(source_run.name) or _attachment_reading_from_run(source_run, recovery_warning=False)
    if not latest:
        return reviewed
    latest_pages = len(_read_pages_for_model_analysis(latest))
    reviewed_pages = len(_read_pages_for_model_analysis(reviewed))
    latest_text_pages = _readable_text_page_count(latest)
    reviewed_text_pages = _readable_text_page_count(reviewed)
    latest_rows = _analysis_row_count(latest)
    reviewed_rows = _analysis_row_count(reviewed)
    if (
        latest_text_pages > reviewed_text_pages
        or latest_pages > reviewed_pages
        or (latest_text_pages == reviewed_text_pages and latest_pages == reviewed_pages and latest_rows > reviewed_rows)
    ):
        latest.setdefault("workflow", {})
        latest["workflow"]["source_payload"] = "latest_financial_attachment_session_reading"
        return latest
    return reviewed


def _readable_text_page_count(payload):
    count = 0
    for group in _analysis_groups(payload):
        if not isinstance(group, dict):
            continue
        for file_info in group.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            for page in file_info.get("pages") or []:
                if isinstance(page, dict) and str(page.get("text_preview") or "").strip():
                    count += 1
    return count


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
        parsed = _attachment_reading_from_session(run.name) or _prefer_latest_source_reading(run, parsed or {})
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
def reprocess_page(run_name, file_id, page):
    """Re-read a single attachment page and merge it back into the extraction run."""
    run = frappe.get_doc("AI Run", run_name)
    _assert_run_access(run)
    page_number = max(1, cint(page) or 1)
    service = FileContextService()
    file_doc = None
    for candidate in service.get_run_files(run.name)[:MAX_FILES]:
        if candidate.name == file_id or candidate.file_name == file_id:
            file_doc = candidate
            break
    if not file_doc:
        frappe.throw(_("File was not found on this run."))

    extension = os.path.splitext(file_doc.file_name or file_doc.name)[1].lower()
    page_result = _extract_page_result(service, file_doc, page_number)
    if page_result is None:
        frappe.throw(_("This file type cannot be re-read page by page."))

    analysis = _extract_json(run.final_answer or "") or _attachment_reading_from_run(run) or {"document_groups": []}
    _replace_page_in_attachment_reading(analysis, file_doc, page_result)
    run.final_answer = json.dumps(analysis, ensure_ascii=False, default=str)
    run.error_summary = None
    _safe_save_run(run)
    return {
        "analysis": analysis,
        "page": page_result,
        "message": _("تمت إعادة قراءة الصفحة وتحديث نتيجة الاستخراج."),
    }


@frappe.whitelist()
def create_purchase_invoice_drafts(run_name, analysis):
    """Create draft Purchase Invoices from reviewed analysis."""
    run = frappe.get_doc("AI Run", run_name)
    _assert_run_access(run)
    if not frappe.has_permission("Purchase Invoice", "create", user=frappe.session.user):
        frappe.throw(_("No create permission on Purchase Invoice."))

    payload = frappe.parse_json(analysis or "{}")
    groups = _analysis_groups(payload)
    if not groups:
        frappe.throw(_("No invoice groups found in reviewed analysis."))

    created = []
    for group in groups:
        duplicate = _find_duplicate_purchase_invoice(group)
        if duplicate:
            created.append({
                "invoice_group_id": group.get("invoice_group_id"),
                "status": "duplicate",
                "duplicate": duplicate,
            })
            continue
        doc = _build_purchase_invoice_doc(run.company, group)
        doc.insert()
        _attach_source_files(run.name, doc)
        doc.add_comment("Comment", _("Created from Purchase Invoice AI Workspace run {0}.").format(run.name))
        created.append({
            "invoice_group_id": group.get("invoice_group_id"),
            "status": "created",
            "name": doc.name,
            "route": f"/app/purchase-invoice/{doc.name}",
        })
    frappe.db.commit()
    return {"results": created}


@frappe.whitelist()
def prepare_journal_entry_analysis(company, analysis):
    from ai_agent.purchase_invoice_ai.journal_analysis import prepare_journal_entry_analysis as service

    return service(company, analysis)


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

    parent_run = current_run_name or source_run_name or None
    if parent_run:
        parent = frappe.get_doc("AI Run", parent_run)
        _assert_run_access(parent)
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
    _assert_run_access(run)
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
            "Use financial_attachments.get_session_ocr_text for session OCR and financial_attachments.list_leaf_accounts for exact leaf account names when account selection is needed. "
            "Invoices, receipts, and vouchers are source evidence; do not turn the request into Purchase Invoice creation unless the user explicitly asks for that. "
            "For a Journal Entry proposal, return the invoice_groups JSON contract defined by the financial-attachments-to-journal-entry skill."
        ),
        "compact_session": (
            "Session purpose: analyze OCR evidence and prepare Journal Entry proposals when requested.\n"
            "Session name: journal_entry_from_ocr_v1\n"
            "Keep the current request and its OCR evidence separate from prior runs.\n"
            "If a prompt contains a Financial Attachment Session name, read its OCR text and produce the final invoice_groups JSON without asking for task clarification.\n"
            "For Journal Entry proposals, answer with the invoice_groups JSON contract defined by the granted skill."
        ),
    }
    if agent_name:
        agent = frappe.get_doc("AI Agent", agent_name)
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
    if run.user != frappe.session.user and "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized."))


def _assert_session_access(session):
    if session.user != frappe.session.user and "System Manager" not in frappe.get_roles(frappe.session.user):
        frappe.throw(_("Not authorized."))


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


def _review_retry_key(source_run_name, reviewed, user_instructions, defaults):
    fingerprint = {
        "version": 4,
        "stage": "review_analysis",
        "source_run": source_run_name,
        "reviewed": reviewed,
        "user_instructions": user_instructions or "",
        "defaults": defaults or {},
    }
    payload = json.dumps(fingerprint, ensure_ascii=False, sort_keys=True, default=str)
    return "purchase_invoice_ai_review:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _analysis_has_extractable_data(payload):
    groups = _analysis_groups(payload)
    if not groups:
        return False
    for group in groups:
        if _primary_rows(group):
            return True
        for file_info in group.get("files") or []:
            for page in file_info.get("pages") or []:
                if (
                    page.get("text_preview")
                    or page.get("metadata")
                    or page.get("totals")
                    or page.get("warnings")
                ):
                    return True
    return False


def _first_analysis_error(payload):
    if isinstance(payload, dict):
        for key in ("errors", "warnings", "questions"):
            values = payload.get(key)
            if isinstance(values, list):
                for value in values:
                    text = str(value or "").strip()
                    if text:
                        return text
            elif values:
                text = str(values or "").strip()
                if text:
                    return text
    return _(
        "تعذر تحليل النص المقروء بالموديل، لذلك لم يتم إنشاء مراجعة محاسبية ناقصة."
    )


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


def _prompt_user_instructions(prompt):
    match = re.search(r"^User instructions:\s*(.*)$", prompt or "", re.M)
    return match.group(1).strip() if match else ""


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
            if page_row.status != "Read" and not text:
                continue
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
    if not files:
        return None
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


def _analysis_schema_text():
    return json.dumps({
        "client_readable_summary": "",
        "questions": [],
        "warnings": [],
        "errors": [],
        "invoice_groups": [{
            "client_readable_summary": "",
            "invoice_group_id": "GROUP-001",
            "document_meta": {
                "document_type": "tax_invoice|simplified_tax_invoice|pos_receipt|payment_voucher|receipt_voucher|bill|financial_document|unknown",
                "document_title": "",
                "supplier_name": "",
                "supplier_tax_id": "",
                "supplier_phone": "",
                "supplier_address": "",
                "document_number": "",
                "document_date": "",
                "currency": "SAR",
                "subtotal": 0,
                "discount_amount": 0,
                "taxable_amount": 0,
                "tax_amount": 0,
                "grand_total": 0,
                "payment_method": "",
                "source_file": "",
                "source_pages": [1],
                "evidence": {
                    "document_number_text": "",
                    "document_date_text": "",
                    "supplier_text": "",
                    "tax_id_text": "",
                    "total_text": ""
                }
            },
            "files": [{"file_id": "", "file_name": "", "pages": [{"page": 1, "text_preview": ""}]}],
            "journal_entry_lines": [{
                "line_no": 1,
                "account": "",
                "account_match_method": "exact|memory|similarity|needs_user_choice",
                "account_confidence": 0,
                "account_suggestions": [],
                "party_type": "",
                "party": "",
                "supplier_action": "matched|create|needs_user_choice|not_applicable",
                "debit": 0,
                "credit": 0,
                "description": "",
                "custom_cash_supplier": "",
                "custom_bill_no": "",
                "custom_bill_date": "",
                "custom_tax_id": "",
                "custom_tax_amount": 0,
                "user_remark": "",
                "cost_center": "",
                "source_file": "",
                "source_page": 0,
                "line_meta": {
                    "document_number": "",
                    "document_date": "",
                    "supplier_name": "",
                    "supplier_tax_id": "",
                    "line_amount": 0,
                    "tax_amount": 0,
                    "source_text": ""
                },
            }],
            "questions": [],
            "warnings": [],
            "errors": [],
        }]
    }, ensure_ascii=False, indent=2)


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
            _assert_run_access(run)
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


def _extract_json(text):
    if not text:
        return None
    cleaned = str(text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    if cleaned.startswith("```"):
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
        if fenced:
            fenced_cleaned = fenced.group(1).strip()
            try:
                return json.loads(fenced_cleaned)
            except Exception:
                pass

    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(cleaned):
        if char not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(cleaned[index:])
        except Exception:
            continue
        candidates.append((_analysis_payload_score(value), end - index, value))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]
    return None


def _analysis_payload_score(value):
    if isinstance(value, dict):
        score = 10
        if any(isinstance(value.get(key), list) for key in ("invoice_groups", "document_groups", "documents", "groups")):
            score += 100
        if _primary_rows(value):
            score += 80
        for group in _analysis_groups(value):
            if isinstance(group, dict) and group.get("journal_entry_lines"):
                score += 120
            elif isinstance(group, dict) and _primary_rows(group):
                score += 40
        if value.get("client_readable_summary"):
            score += 10
        return score
    if isinstance(value, list):
        return 50 if value else 1
    return 0


def _analysis_groups(payload):
    group_keys = ["document_groups", "documents", "invoice_groups", "groups"]
    if isinstance(payload, dict) and isinstance(payload.get("invoice_groups"), list):
        return payload.get("invoice_groups") or []
    if isinstance(payload, dict):
        for key in group_keys:
            if isinstance(payload.get(key), list):
                return payload.get(key) or []
    if isinstance(payload, dict) and _primary_rows(payload):
        return [payload]
    if isinstance(payload, list):
        return payload
    return []


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


def _primary_rows(group):
    if not isinstance(group, dict):
        return []
    first_array = None
    for key in ("journal_entry_lines", "entry_lines", "accounting_lines", "lines", "extracted_lines", "observed_lines", "items"):
        if isinstance(group.get(key), list):
            if first_array is None:
                first_array = group.get(key) or []
            if group.get(key):
                return group.get(key) or []
    return first_array or []


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


def _valid_document_number(value, tax_identifier=None):
    value = str(value or "").strip()
    if not _is_plausible_document_number(value):
        return ""
    if tax_identifier and value == str(tax_identifier or "").strip():
        return ""
    return value


def _normalize_analysis_payload(payload):
    if isinstance(payload, dict) and isinstance(payload.get("invoice_groups"), list):
        return payload
    if isinstance(payload, dict):
        for key in ("document_groups", "documents", "groups"):
            if isinstance(payload.get(key), list):
                normalized = dict(payload)
                normalized["invoice_groups"] = payload.get(key) or []
                return normalized
    if isinstance(payload, list):
        return {"invoice_groups": payload}
    if isinstance(payload, dict):
        return {"invoice_groups": [payload]}
    return {"invoice_groups": []}


def _canonicalize_journal_payload(payload, allow_page_fallback=False):
    normalized = _normalize_analysis_payload(payload)
    for index, group in enumerate(normalized.get("invoice_groups") or [], start=1):
        if not isinstance(group, dict):
            continue
        group.setdefault("invoice_group_id", f"GROUP-{index:03d}")
        rows = group.get("journal_entry_lines") if isinstance(group.get("journal_entry_lines"), list) else _primary_rows(group)
        canonical_rows = []
        for line_no, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            canonical_rows.append(_canonical_journal_line(row, line_no))
        group["journal_entry_lines"] = canonical_rows
    return normalized


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


def _analysis_row_count(payload):
    count = 0
    for group in _analysis_groups(_normalize_analysis_payload(payload)):
        count += len(_primary_rows(group))
    return count


def _canonical_journal_line(row, line_no):
    canonical = dict(row)
    canonical["line_no"] = cint(row.get("line_no")) or line_no
    canonical["account"] = str(row.get("account") or "")
    canonical["account_match_method"] = str(row.get("account_match_method") or "")
    canonical["account_confidence"] = 0
    canonical["account_suggestions"] = []
    canonical["party_type"] = str(row.get("party_type") or "")
    canonical["party"] = str(row.get("party") or "")
    canonical["supplier_action"] = str(row.get("supplier_action") or "")
    canonical["debit"] = flt(row.get("debit"))
    canonical["credit"] = flt(row.get("credit"))
    canonical["description"] = str(row.get("description") or "")
    canonical["custom_cash_supplier"] = str(row.get("custom_cash_supplier") or "")
    canonical["custom_bill_no"] = str(row.get("custom_bill_no") or "")
    canonical["custom_bill_date"] = str(row.get("custom_bill_date") or "")
    canonical["custom_tax_id"] = str(row.get("custom_tax_id") or "")
    canonical["custom_tax_amount"] = flt(row.get("custom_tax_amount"))
    canonical["user_remark"] = str(row.get("user_remark") or "")
    canonical["cost_center"] = str(row.get("cost_center") or "")
    canonical["source_file"] = str(row.get("source_file") or "")
    canonical["source_page"] = cint(row.get("source_page"))
    canonical["line_meta"] = row.get("line_meta") if isinstance(row.get("line_meta"), dict) else {}
    return canonical


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


def _build_purchase_invoice_doc(company, group):
    supplier = (group.get("supplier") or {}).get("matched_supplier") or group.get("supplier")
    invoice = group.get("invoice") or {}
    if not supplier:
        frappe.throw(_("Supplier is required before creating a Purchase Invoice."))
    items = group.get("items") or []
    if not items:
        frappe.throw(_("At least one item row is required."))

    doc = frappe.get_doc({
        "doctype": "Purchase Invoice",
        "company": company,
        "supplier": supplier,
        "bill_no": invoice.get("bill_no"),
        "bill_date": invoice.get("bill_date") or invoice.get("posting_date") or nowdate(),
        "posting_date": invoice.get("posting_date") or invoice.get("bill_date") or nowdate(),
        "due_date": invoice.get("due_date"),
        "currency": invoice.get("currency"),
        "remarks": invoice.get("remarks"),
        "items": [_purchase_invoice_item(row) for row in items],
    })
    for field in ("set_warehouse", "cost_center", "project", "conversion_rate"):
        if invoice.get(field):
            doc.set(field, invoice.get(field))
    return doc


def _purchase_invoice_item(row):
    item_code = row.get("item_code") or row.get("matched_item_code")
    if not item_code:
        frappe.throw(_("Every row must have an item_code before draft creation."))
    item = {
        "item_code": item_code,
        "description": row.get("description") or row.get("extracted_item_name") or item_code,
        "qty": flt(row.get("qty")) or 1,
        "rate": flt(row.get("rate")),
    }
    optional_fields = ("uom", "warehouse", "expense_account", "cost_center", "project", "purchase_order")
    for field in optional_fields:
        if row.get(field):
            item[field] = row.get(field)
    return item


def _find_duplicate_purchase_invoice(group):
    supplier = (group.get("supplier") or {}).get("matched_supplier") or group.get("supplier")
    bill_no = (group.get("invoice") or {}).get("bill_no")
    if not supplier or not bill_no:
        return None
    existing = frappe.get_all(
        "Purchase Invoice",
        filters={"supplier": supplier, "bill_no": bill_no, "docstatus": ("<", 2)},
        fields=["name", "posting_date", "grand_total"],
        limit=1,
    )
    return existing[0] if existing else None


def _attach_source_files(run_name, target_doc):
    for file_info in _run_files(run_name):
        try:
            file_doc = frappe.get_doc("File", file_info["name"])
            with open(get_file_path(file_doc.name), "rb") as handle:
                content = handle.read()
            save_file(file_doc.file_name, content, target_doc.doctype, target_doc.name, is_private=1)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Purchase Invoice AI file attach failed")


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
            job_name=f"financial-attachment-page-sequence-{run_name}",
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


def execute_sequential_page_reading_job(run_name, selected_pages, user=None):
    """Process existing Financial Attachment Session pages one by one."""
    frappe.set_user(user or "Administrator")
    ordered_pages = sorted(
        selected_pages or [],
        key=lambda row: (str(row.get("file_id") or ""), cint(row.get("page")) or 1),
    )
    for page_item in ordered_pages:
        if _should_stop_page_reading(run_name):
            _cancel_page_reading_session(run_name)
            return
        _complete_single_page_reading(run_name, page_item)
        # Persist the page result before starting the next isolated OCR process.
        frappe.db.commit()
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
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "Financial Attachment Single Page Read Failed")
        _mark_single_page_failed(run_name, page_item, _("تعذرت قراءة هذه الصفحة: {0}").format(str(exc)))
        try:
            trace.complete_step(step.name, status="Failed", summary=_("تعذرت قراءة هذه الصفحة."))
        except Exception:
            pass


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
    raw_ocr = _extract_json(run.final_answer or "") or _attachment_reading_from_run(run, recovery_warning=False) or {"document_groups": []}
    run.status = "Completed"
    run.final_answer = json.dumps(raw_ocr, ensure_ascii=False, default=str)
    run.current_step = 1
    run.tool_call_count = 0
    run.completed_at = frappe.utils.now_datetime()
    _safe_save_run(run)
    _publish_page_reading_update(run_name)
    TraceService().publish_realtime("ai_agent:run_completed", run_name)


def _truncate_for_analysis(value, limit=MAX_ANALYSIS_ROW_TEXT_CHARS):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... [truncated for chunked analysis]"


def _compact_ai_context(compact_ctx):
    if not isinstance(compact_ctx, dict):
        return compact_ctx
    ctx = {}
    ctx["leaf_expense_accounts"] = [
        {
            "name": item.get("name"),
            "account_name": item.get("account_name"),
            "number": item.get("number") or item.get("account_number"),
        }
        for item in (compact_ctx.get("leaf_expense_accounts") or [])[:MAX_MODEL_CONTEXT_ACCOUNTS]
        if isinstance(item, dict)
    ]
    ctx["cash_suppliers"] = [
        {
            "name": item.get("name"),
            "supplier_name": item.get("supplier_name"),
            "tax_id": item.get("tax_id"),
        }
        for item in (compact_ctx.get("cash_suppliers") or [])[:MAX_MODEL_CONTEXT_SUPPLIERS]
        if isinstance(item, dict)
    ]
    ctx["cost_centers"] = [
        {
            "name": item.get("name"),
            "cost_center_name": item.get("cost_center_name"),
        }
        for item in (compact_ctx.get("cost_centers") or [])[:MAX_MODEL_CONTEXT_COST_CENTERS]
        if isinstance(item, dict)
    ]
    memories = []
    for item in (compact_ctx.get("ai_memories") or [])[:MAX_MODEL_CONTEXT_MEMORIES]:
        if isinstance(item, dict):
            memories.append({
                "supplier": item.get("supplier") or item.get("supplier_name"),
                "account": item.get("account"),
                "description": _truncate_for_analysis(item.get("description") or item.get("content") or "", 240),
            })
        elif item:
            memories.append(_truncate_for_analysis(item, 300))
    ctx["ai_memories"] = memories
    return ctx


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _model_timeout_seconds(default=120):
    try:
        configured = cint(frappe.db.get_single_value("AI Settings", "request_timeout"))
    except Exception:
        configured = 0
    return max(60, min(configured or default, 240))


def _compact_payload_text_fields(payload):
    if isinstance(payload, dict):
        for key, value in list(payload.items()):
            if key in {"source_text", "text_preview", "raw_text", "ocr_text"} and isinstance(value, str):
                payload[key] = _truncate_for_analysis(value, 4000)
            else:
                _compact_payload_text_fields(value)
    elif isinstance(payload, list):
        for item in payload:
            _compact_payload_text_fields(item)
    return payload


def _read_pages_for_model_analysis(payload):
    pages = []
    for group_index, group in enumerate(_analysis_groups(payload), start=1):
        if not isinstance(group, dict):
            continue
        for file_info in group.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            file_id = file_info.get("file_id") or file_info.get("name") or ""
            file_name = file_info.get("file_name") or file_info.get("name") or file_id
            for page in file_info.get("pages") or []:
                if not isinstance(page, dict):
                    continue
                text = str(page.get("text_preview") or "").strip()
                if not text:
                    continue
                item = {
                    "source_group": group_index,
                    "source_page_id": page.get("page_id") or "",
                    "source_file": file_name,
                    "source_page": cint(page.get("page")) or 1,
                    "status": page.get("status") or "",
                    "extraction": page.get("extraction") or "",
                    "text": _truncate_for_analysis(text, MAX_MODEL_PAGE_TEXT_CHARS),
                    "text_preview": _truncate_for_analysis(text, MAX_MODEL_PAGE_TEXT_CHARS),
                    "warnings": page.get("warnings") or [],
                    "extracted_lines_count": cint(page.get("extracted_lines_count")),
                }
                totals = page.get("totals") or {}
                metadata = page.get("metadata") or {}
                if totals:
                    item["ocr_totals"] = totals
                if metadata:
                    item["ocr_meta"] = metadata
                pages.append(item)
    return pages


def _chunk_pages_for_model(pages):
    pages = pages or []
    if not pages:
        return []
    if len(pages) <= MAX_ANALYSIS_PAGES_PER_CHUNK and len(_compact_json(pages)) <= MAX_ANALYSIS_PAGE_CHUNK_CHARS:
        return [pages]
    chunks = []
    current = []
    current_chars = 0
    for page in pages:
        page_chars = len(_compact_json(page))
        if current and (
            len(current) >= MAX_ANALYSIS_PAGES_PER_CHUNK
            or current_chars + page_chars > MAX_ANALYSIS_PAGE_CHUNK_CHARS
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(page)
        current_chars += page_chars
    if current:
        chunks.append(current)
    return chunks


def _process_page_chunk_with_ai(company, compact_ctx, page_chunk, user_instructions="", chunk_index=1, total_chunks=1):
    try:
        from ai_agent.runtime.model_provider import get_provider
        provider = get_provider()
    except Exception as exc:
        frappe.log_error(
            frappe.get_traceback(),
            f"Financial Attachment Page Chunk AI Provider Failed {chunk_index}/{total_chunks}",
        )
        return {
            "errors": [
                _("تعذر تجهيز مزود نموذج التحليل: {0}").format(str(exc)[:300])
            ],
            "workflow": {"analysis_failed": 1},
        }

    context_json = _compact_json(compact_ctx)
    page_json = _compact_json(page_chunk)
    context_block = f"ERP CONTEXT JSON:\n{context_json}\n\n" if compact_ctx else ""
    skill_block = _financial_attachment_skill_instructions()
    prompt = (
        "FINANCIAL_ATTACHMENT_PAGE_ANALYSIS\n"
        f"Company: {company}\n"
        f"Chunk: {chunk_index}/{total_chunks}\n"
        f"User instructions: {user_instructions or 'None'}\n\n"
        "Apply the skill/model instructions below as the source of extraction, matching, and accounting behavior.\n"
        "Return JSON only. Do not add Markdown fences or prose outside JSON.\n\n"
        f"SKILL INSTRUCTIONS:\n{skill_block}\n\n"
        f"{context_block}"
        f"REQUIRED JSON CONTRACT:\n{_analysis_schema_text()}\n\n"
        f"PAGE OCR JSON:\n{page_json}\n\n"
        "Return ONLY one valid JSON object. The top-level key must be invoice_groups."
    )

    try:
        response = provider.create_step(
            instructions={
                "messages": [
                    {
                        "role": "system",
                        "content": "You are the page-level worker for a Frappe ERP accounting agent. Follow the supplied skill instructions exactly and return valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 6000,
                "timeout": _model_timeout_seconds(),
                "max_retries": 1,
                "response_format": {"type": "json_object"},
            },
            input_items=[],
            tools=[],
            state={},
        )
        content = _model_message_content(response)
        parsed = _extract_json(content)
        if _payload_has_journal_rows(parsed):
            return parsed
        repaired = _repair_page_chunk_payload(
            provider,
            company=company,
            compact_ctx=compact_ctx,
            page_chunk=page_chunk,
            previous_content=content,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
        )
        if _payload_has_journal_rows(repaired):
            return repaired
        return {
            "errors": [
                _("رد الموديل للدفعة {0} لم يحتوي على سطور قيد قابلة للعرض.").format(chunk_index)
            ],
            "warnings": [_("تم حفظ ملخص رد الموديل للتشخيص، ولم يتم إنشاء سطور من OCR عبر الكود.")],
            "workflow": {
                "analysis_failed": 1,
                "parsed_shape": _payload_shape(parsed),
                "repaired_shape": _payload_shape(repaired),
                "model_response_preview": str(content or "")[:4000],
            },
        }
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), f"Financial Attachment Page Chunk AI Failed {chunk_index}/{total_chunks}")
        return {
            "errors": [
                _("تعذر تحليل الدفعة {0} من {1} بالموديل: {2}").format(
                    chunk_index,
                    total_chunks,
                    str(exc)[:300],
                )
            ],
            "workflow": {"analysis_failed": 1},
        }


def _model_message_content(response):
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return message.get("content") or ""
        if response.get("content"):
            return response.get("content") or ""
    return ""


def _payload_has_journal_rows(payload):
    return _analysis_row_count(payload) > 0


def _payload_shape(payload):
    if isinstance(payload, dict):
        return {
            "type": "dict",
            "keys": list(payload.keys())[:20],
            "group_count": len(_analysis_groups(payload)),
            "row_count": _analysis_row_count(payload),
        }
    if isinstance(payload, list):
        return {"type": "list", "items": len(payload), "row_count": _analysis_row_count(payload)}
    return {"type": type(payload).__name__, "row_count": 0}


def _financial_attachment_skill_instructions():
    instructions = frappe.db.get_value("AI Skill", FINANCIAL_ATTACHMENTS_SKILL, "instructions") or ""
    return _truncate_for_analysis(instructions, 30000)


def _group_matches_fixed_journal_contract(group):
    if not isinstance(group, dict):
        return False
    if not isinstance(group.get("document_meta"), dict):
        return False
    rows = group.get("journal_entry_lines")
    if not isinstance(rows, list) or not rows:
        return False
    required_group_meta = {
        "document_type", "document_title", "supplier_name", "supplier_tax_id",
        "document_number", "document_date", "currency", "subtotal", "tax_amount",
        "grand_total", "source_file", "source_pages",
    }
    if not required_group_meta.issubset(set(group["document_meta"].keys())):
        return False
    row_required = {
        "line_no", "account", "account_match_method", "account_confidence",
        "account_suggestions", "party_type", "party", "supplier_action", "debit",
        "credit", "description", "custom_cash_supplier", "custom_bill_no",
        "custom_bill_date", "custom_tax_id", "custom_tax_amount", "user_remark",
        "cost_center", "source_file", "source_page", "line_meta",
    }
    forbidden_row_keys = {
        "invoice_no", "invoice_number", "document_number", "document_date",
        "amount", "line_amount", "line_total", "net_amount", "total_amount",
        "tax_id", "vat_number", "tax_identifier", "supplier_name", "supplier",
        "expense_account", "debit_account", "bill_no", "bill_date",
    }
    line_meta_required = {
        "document_number", "document_date", "supplier_name", "supplier_tax_id",
        "line_amount", "tax_amount", "source_text",
    }
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not row_required.issubset(set(row.keys())):
            return False
        if forbidden_row_keys.intersection(row.keys()):
            return False
        if not isinstance(row.get("line_meta"), dict):
            return False
        if not line_meta_required.issubset(set(row["line_meta"].keys())):
            return False
    return True


def _repair_page_chunk_payload(provider, *, company, compact_ctx, page_chunk, previous_content, chunk_index, total_chunks):
    context_json = _compact_json(compact_ctx)
    page_json = _compact_json(page_chunk)
    repair_prompt = (
        f"Your previous response for financial attachment chunk {chunk_index} of {total_chunks} did not match the required JSON contract.\n"
        "Return one valid JSON object only. Do not add prose or Markdown.\n"
        "Required: top-level `invoice_groups` array only. Do not use document_groups/documents/groups/items/extracted_lines as substitutes.\n"
        "Each group must include a non-empty `journal_entry_lines` array when the OCR page contains a financial document.\n"
        "Every group must include `document_meta`. Every journal_entry_lines row must include only the fixed row field names from the JSON contract and must include `line_meta`.\n"
        "Copy document_meta document_number/date/supplier/tax id/totals into the canonical row fields custom_bill_no, custom_bill_date, custom_cash_supplier, custom_tax_id, debit, custom_tax_amount.\n"
        "Choose only one best account in row.account. Keep account_suggestions empty and account_confidence 0. Do not return reasons, percentages, or alternative accounts.\n"
        "Do not invent values. Use visible OCR evidence only. Preserve source_file and source_page on every row.\n\n"
        f"Company: {company}\n"
        f"ERP CONTEXT JSON:\n{context_json}\n\n"
        f"REQUIRED JSON CONTRACT:\n{_analysis_schema_text()}\n\n"
        f"PAGE OCR JSON:\n{page_json}\n\n"
        f"PREVIOUS RESPONSE TO FIX:\n{str(previous_content or '')[:5000]}"
    )
    try:
        response = provider.create_step(
            instructions={
                "messages": [
                    {
                        "role": "system",
                        "content": "You repair malformed ERP accounting extraction JSON. Return ONLY valid JSON.",
                    },
                    {"role": "user", "content": repair_prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 1800,
                "timeout": _model_timeout_seconds(),
                "max_retries": 1,
                "response_format": {"type": "json_object"},
            },
            input_items=[],
            tools=[],
            state={},
        )
        return _extract_json(_model_message_content(response))
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Financial Attachment Page Chunk AI Repair Failed {chunk_index}/{total_chunks}")
        return {}


def _merge_page_chunk_payloads(payloads):
    merged = {
        "client_readable_summary": "",
        "questions": [],
        "invoice_groups": [],
        "warnings": [],
        "errors": [],
    }
    summaries = []
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        if payload.get("client_readable_summary"):
            summaries.append(str(payload.get("client_readable_summary")))
        for key in ("questions", "warnings", "errors"):
            values = payload.get(key)
            if isinstance(values, list):
                merged[key].extend(values)
        for group in _analysis_groups(payload):
            if isinstance(group, dict):
                merged["invoice_groups"].append(group)
        workflow = payload.get("workflow")
        if isinstance(workflow, dict) and workflow.get("analysis_failed"):
            merged.setdefault("workflow", {})["analysis_failed"] = 1
    if summaries:
        merged["client_readable_summary"] = "\n".join(summaries)
    for index, group in enumerate(merged["invoice_groups"], start=1):
        group.setdefault("invoice_group_id", f"GROUP-{index:03d}")
        group.setdefault("document_group_id", group.get("invoice_group_id"))
    return merged


def _analysis_failed_payload(message, errors=None, warnings=None):
    return {
        "document_groups": [],
        "warnings": list(warnings or []),
        "errors": [message] + list(errors or []),
        "workflow": {
            "stage": "model_page_chunk_analysis",
            "analysis_failed": 1,
            "next_step": "fix_model_provider_or_ocr_and_retry",
        },
    }


def _prepare_incremental_analysis(company, chunk_results, *, total_chunks, completed_chunks, failed_chunks=0, errors=None):
    merged = _merge_page_chunk_payloads(chunk_results)
    workflow = merged.setdefault("workflow", {})
    workflow.update({
        "stage": "model_page_chunk_analysis",
        "is_partial": completed_chunks < total_chunks,
        "progress": {
            "total_chunks": cint(total_chunks),
            "completed_chunks": cint(completed_chunks),
            "failed_chunks": cint(failed_chunks),
            "percent": round((cint(completed_chunks) / max(cint(total_chunks), 1)) * 100, 2),
        },
    })
    if errors:
        merged.setdefault("errors", [])
        merged["errors"].extend(errors)
    if failed_chunks:
        workflow["failed_chunks"] = cint(failed_chunks)
    if not _analysis_groups(merged):
        merged["workflow"] = workflow
        return merged
    analyzed = _canonicalize_journal_payload(merged, allow_page_fallback=False)
    analyzed = _prepare_journal_payload_for_review(analyzed, company)
    analyzed["workflow"] = workflow
    analyzed = _compact_payload_text_fields(analyzed)
    return analyzed


def _execute_chunked_analysis_for_run(run):
    """Execute page-level AI analysis for a given AI Run record, then consolidate."""
    trace = TraceService()
    run.status = "Running"
    run.current_step = 1
    _safe_save_run(run)
    trace.publish_realtime("ai_agent:run_updated", run.name)

    start_step = trace.add_step(
        run.name,
        1,
        "Plan",
        _("تجهيز تحليل الجلسة على مستوى الصفحات"),
        _("سيتم تحليل كل صفحة مقروءة وحفظ نتيجتها ثم تجميع النتائج في قيد يومية."),
    )

    source_analysis = None
    if run.parent_run:
        source_analysis = _attachment_reading_from_session(run.parent_run)
        if not source_analysis:
            parent = frappe.get_doc("AI Run", run.parent_run)
            source_analysis = _extract_json(parent.final_answer or "")
    if not source_analysis:
        source_analysis = _extract_json(run.final_answer or "")
    if not source_analysis:
        source_analysis = (
            _attachment_reading_from_session(run.name)
            or _attachment_reading_from_run(run, recovery_warning=False)
            or {"document_groups": []}
        )

    pages = _read_pages_for_model_analysis(source_analysis)
    page_chunks = _chunk_pages_for_model(pages)
    unread_summary = _session_unread_page_summary(run.parent_run or run.name)
    trace.complete_step(
        start_step.name,
        summary=_("الصفحات المقروءة للتحليل: {0}. الصفحات غير المقروءة: {1}.").format(
            len(pages),
            unread_summary.get("unread_count", 0),
        ),
    )
    if not page_chunks:
        analysis = _analysis_failed_payload(_("لا توجد قراءة OCR كافية لإرسالها إلى نموذج التحليل."))
    else:
        chunk_results = []
        chunk_errors = []
        failed_chunks = 0
        total_chunks = len(page_chunks)
        compact_ctx = _build_compact_context(run.company)
        for chunk in page_chunks:
            _store_page_analysis_result(run.parent_run or run.name, run.name, chunk, {}, "Pending")
        for index, chunk in enumerate(page_chunks, start=1):
            run.reload()
            if run.status in TERMINAL_STATUSES or run.cancel_requested:
                return
            page_label = _page_chunk_label(chunk, index)
            step = trace.add_step(
                run.name,
                index + 1,
                "Tool",
                _("تحليل {0}").format(page_label),
                _("إرسال بيانات OCR وسياق ERP لهذه الصفحة إلى الوكيل."),
            )
            _store_page_analysis_result(run.parent_run or run.name, run.name, chunk, {}, "Running")
            result = _process_page_chunk_with_ai(
                company=run.company,
                compact_ctx=compact_ctx,
                page_chunk=chunk,
                user_instructions=_prompt_user_instructions(run.prompt),
                chunk_index=index,
                total_chunks=total_chunks,
            )
            page_status = "Completed"
            if isinstance(result, dict) and (result.get("workflow") or {}).get("analysis_failed"):
                failed_chunks += 1
                page_status = "Failed"
                for value in result.get("errors") or []:
                    chunk_errors.append(str(value))
            elif result:
                chunk_results.append(result)
            _store_page_analysis_result(run.parent_run or run.name, run.name, chunk, result, page_status)
            trace.complete_step(
                step.name,
                status=page_status,
                summary=_page_analysis_step_summary(result),
            )

            if chunk_results:
                partial = _prepare_incremental_analysis(
                    run.company,
                    chunk_results,
                    total_chunks=total_chunks,
                    completed_chunks=index,
                    failed_chunks=failed_chunks,
                    errors=chunk_errors,
                )
                run.reload()
                run.status = "Running"
                run.final_answer = json.dumps(partial, ensure_ascii=False, default=str)
                run.current_step = index + 1
                run.tool_call_count = index
                _safe_save_run(run)
                trace.publish_realtime("ai_agent:run_updated", run.name)

        if not chunk_results:
            analysis = _analysis_failed_payload(
                _("تعذر تحليل النص المقروء بالموديل، لذلك لم يتم إنشاء مراجعة محاسبية ناقصة."),
                errors=chunk_errors,
            )
        else:
            merged_analysis = _prepare_incremental_analysis(
                run.company,
                chunk_results,
                total_chunks=total_chunks,
                completed_chunks=total_chunks,
                failed_chunks=failed_chunks,
                errors=chunk_errors,
            )
            final_step = trace.add_step(
                run.name,
                total_chunks + 2,
                "Final",
                _("تجميع نتائج الصفحات وتجهيز القيد"),
                _("إرسال نتائج الصفحات المحفوظة للوكيل لتجهيز القيد النهائي."),
            )
            analysis = _consolidate_page_results_with_ai(
                company=run.company,
                compact_ctx=compact_ctx,
                page_results=chunk_results,
                merged_analysis=merged_analysis,
                user_instructions=_prompt_user_instructions(run.prompt),
            ) or merged_analysis
            trace.complete_step(
                final_step.name,
                summary=_("تم تجهيز {0} سطر/سطور قيد من نتائج الصفحات.").format(_analysis_row_count(analysis)),
            )
            if failed_chunks:
                analysis.setdefault("workflow", {})["analysis_failed"] = 1
    if unread_summary.get("unread_count"):
        analysis.setdefault("warnings", []).append(unread_summary.get("message"))
        analysis.setdefault("workflow", {})["unread_pages"] = unread_summary
    has_data = _analysis_has_extractable_data(analysis)
    analysis_failed = bool(isinstance(analysis, dict) and (analysis.get("workflow") or {}).get("analysis_failed"))

    step_status = "Failed" if analysis_failed or not has_data else "Completed"
    step_summary = _first_analysis_error(analysis) if analysis_failed else _extraction_message(has_data)

    run.reload()
    run.status = "Failed" if analysis_failed or not has_data else "Completed"
    run.final_answer = json.dumps(analysis, ensure_ascii=False, default=str)
    run.error_summary = step_summary if run.status == "Failed" else ""
    run.current_step = len(page_chunks) + 2
    run.tool_call_count = len(page_chunks)
    run.completed_at = frappe.utils.now_datetime()
    _safe_save_run(run)
    event_name = "ai_agent:run_failed" if run.status == "Failed" else "ai_agent:run_completed"
    trace.publish_realtime(event_name, run.name)


def _page_chunk_label(chunk, fallback_index):
    page = (chunk or [{}])[0] if isinstance(chunk, list) else {}
    source_page = page.get("source_page") if isinstance(page, dict) else None
    source_file = page.get("source_file") if isinstance(page, dict) else None
    if source_page:
        return _("صفحة {0}{1}").format(source_page, f" - {source_file}" if source_file else "")
    return _("دفعة {0}").format(fallback_index)


def _page_analysis_step_summary(result):
    if not isinstance(result, dict):
        return _("لم يرجع الوكيل نتيجة منظمة لهذه الصفحة.")
    errors = result.get("errors") or []
    if errors:
        return str(errors[0])[:500]
    return _("رجعت الصفحة {0} مجموعة و {1} سطر/سطور قيد.").format(
        len(_analysis_groups(result)),
        _analysis_row_count(result),
    )


def _store_page_analysis_result(source_run_name, analysis_run_name, page_chunk, result, status):
    if not _has_session_page_analysis_columns():
        return
    session = _session_for_run(source_run_name)
    if not session:
        return
    page_ids = {
        str(page.get("source_page_id") or page.get("page_id") or "")
        for page in page_chunk or []
        if isinstance(page, dict) and (page.get("source_page_id") or page.get("page_id"))
    }
    page_refs = {
        (
            str(page.get("source_file") or ""),
            cint(page.get("source_page")),
        )
        for page in page_chunk or []
        if isinstance(page, dict)
    }
    payload = json.dumps(result or {}, ensure_ascii=False, default=str)
    for row in session.pages:
        if str(row.page_id or "") not in page_ids and (str(row.file_name or ""), cint(row.page_number)) not in page_refs:
            continue
        frappe.db.set_value(
            "Financial Attachment Session Page",
            row.name,
            {
                "analysis_status": status,
                "analysis_run": analysis_run_name,
                "analysis_json": payload,
            },
            update_modified=False,
        )
    frappe.db.commit()


def _has_session_page_analysis_columns():
    return all(
        frappe.db.has_column("Financial Attachment Session Page", field)
        for field in ("analysis_status", "analysis_run", "analysis_json")
    )


def _session_unread_page_summary(source_run_name):
    session = _session_for_run(source_run_name)
    if not session:
        return {"unread_count": 0, "message": ""}
    unread = [
        cint(row.page_number)
        for row in session.pages
        if row.can_read and row.status != "Read"
    ]
    if not unread:
        return {"unread_count": 0, "message": ""}
    pages = ", ".join(str(page) for page in unread[:20])
    if len(unread) > 20:
        pages += ", ..."
    return {
        "unread_count": len(unread),
        "pages": unread,
        "message": _("لم تتم معالجة {0} صفحة لأنها لم تُقرأ OCR بعد: {1}. اقرأ الصفحات أولًا ثم أعد المعالجة.").format(
            len(unread),
            pages,
        ),
    }


def _consolidate_page_results_with_ai(company, compact_ctx, page_results, merged_analysis, user_instructions=""):
    try:
        from ai_agent.runtime.model_provider import get_provider
        provider = get_provider()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Financial Attachment Consolidation AI Provider Failed")
        return None

    prompt = (
        "FINANCIAL_ATTACHMENT_FINAL_CONSOLIDATION\n"
        f"Company: {company}\n"
        f"User instructions: {user_instructions or 'None'}\n\n"
        "Apply the skill/model instructions below as the source of consolidation and accounting behavior.\n"
        "Use the page-level analysis results as persisted worker output. Return JSON only.\n\n"
        f"SKILL INSTRUCTIONS:\n{_financial_attachment_skill_instructions()}\n\n"
        f"ERP CONTEXT JSON:\n{_truncate_for_analysis(_compact_json(compact_ctx), 120000)}\n\n"
        f"REQUIRED JSON CONTRACT:\n{_analysis_schema_text()}\n\n"
        f"MERGED PAGE ANALYSIS JSON:\n{_truncate_for_analysis(_compact_json(merged_analysis), 260000)}\n\n"
        f"PAGE RESULTS JSON:\n{_truncate_for_analysis(_compact_json(page_results), 260000)}"
    )
    try:
        response = provider.create_step(
            instructions={
                "messages": [
                    {
                        "role": "system",
                        "content": "You are the final consolidation worker for a Frappe ERP accounting agent. Follow the supplied skill instructions exactly and return valid JSON only.",
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
                "max_tokens": 8000,
                "timeout": _model_timeout_seconds(),
                "max_retries": 1,
                "response_format": {"type": "json_object"},
            },
            input_items=[],
            tools=[],
            state={},
        )
        parsed = _extract_json(_model_message_content(response))
        if _analysis_row_count(parsed):
            return _compact_payload_text_fields(
                _prepare_journal_payload_for_review(
                    _canonicalize_journal_payload(parsed, allow_page_fallback=False),
                    company,
                )
            )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Financial Attachment Final Consolidation Failed")
    return None

def _start_background_run(run_name, user, site, enqueue_after_commit=True):
    try:
        frappe.enqueue(
            "ai_agent.purchase_invoice_ai.api.execute_run_job",
            queue="long",
            timeout=1800,
            job_name=f"purchase-invoice-ai-{run_name}",
            run_name=run_name,
            user=user,
            enqueue_after_commit=enqueue_after_commit,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Purchase Invoice AI Run Enqueue Failed")


def execute_run_job(run_name, user=None):
    frappe.set_user(user or "Administrator")
    try:
        run = frappe.get_doc("AI Run", run_name)
        if run.status in TERMINAL_STATUSES or run.cancel_requested:
            return
        if run.prompt and (
            "FINANCIAL_ATTACHMENT_PAGE_PIPELINE" in run.prompt
            or "REVIEWED EXTRACTED PAYLOAD" in run.prompt
            or "INPUT PAYLOAD TO ANALYZE" in run.prompt
        ):
            _execute_chunked_analysis_for_run(run)
        else:
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
