import json
import os

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.file_manager import get_file_path

from ai_agent.runtime.file_context import FileContextService


def start_extraction(company, files, grouping_mode="auto", user_instructions="", defaults=None):
    """Upload attachments and split them into reviewable pages without OCR/model calls."""
    from ai_agent.purchase_invoice_ai import api as legacy

    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot create runs."))
    legacy._assert_read("Company")
    legacy._assert_company_access(company)

    files = frappe.parse_json(files or "[]")
    defaults = frappe.parse_json(defaults or "{}")
    if not isinstance(files, list) or not files:
        frappe.throw(_("Attach at least one file."))
    if len(files) > legacy.MAX_FILES:
        frappe.throw(_("You can attach up to {0} files.").format(legacy.MAX_FILES))

    prepared_files = legacy._prepare_uploads(files)
    retry_key = legacy._purchase_retry_key(
        stage="extraction",
        company=company,
        grouping_mode=grouping_mode,
        user_instructions=user_instructions,
        defaults=defaults,
        prepared_files=prepared_files,
    )
    legacy._ensure_purchase_invoice_agent()
    run = frappe.get_doc({
        "doctype": "AI Run",
        "agent": legacy.PURCHASE_AGENT,
        "user": frappe.session.user,
        "company": company,
        "prompt": "Preparing financial attachment pages...",
        "status": "Running",
        "started_at": frappe.utils.now_datetime(),
        "idempotency_key": retry_key,
    })
    run.insert(ignore_permissions=True)

    saved_files = legacy._save_run_files(run.name, prepared_files)
    pages = legacy._attachment_page_manifest(run.name)
    session = legacy._create_financial_attachment_session(
        run=run,
        company=company,
        grouping_mode=grouping_mode,
        user_instructions=user_instructions,
        saved_files=saved_files,
        pages=pages,
    )
    ui_contract = legacy._skill_ui_contract()
    run.prompt = (
        f"{ui_contract.get('page_title') or 'AI attachment workspace'} extraction preview.\n"
        f"Skill: {legacy.FINANCIAL_ATTACHMENTS_SKILL}\n"
        f"Company: {company or ''}\n"
        f"Grouping mode: {grouping_mode or 'auto'}\n"
        f"User instructions: {user_instructions or ''}\n"
        f"Defaults: {json.dumps(defaults or {}, ensure_ascii=False, default=str)}\n"
        "Stage: attachment_page_preparation_only\n"
    )
    run.final_answer = json.dumps({
        "invoice_groups": [{
            "document_group_id": "GROUP-001",
            "files": legacy._manifest_files_for_storage(pages),
            "extracted_lines": [],
            "warnings": [],
        }],
        "workflow": {
            "stage": "page_preparation",
            "next_step": "select_pages_for_ocr",
        },
    }, ensure_ascii=False, default=str)
    run.status = "Completed"
    run.completed_at = frappe.utils.now_datetime()
    legacy._safe_save_run(run)

    return {
        "session_name": session.name,
        "run_name": run.name,
        "status": "pages_ready",
        "files": saved_files,
        "pages": pages,
        "analysis": legacy._extract_json(run.final_answer or ""),
        "extraction_status": "pages_ready",
        "message": _("تم تفكيك المرفقات إلى صفحات. اختر الصفحات التي تريد قراءتها."),
    }


def read_selected_pages(run_name, pages):
    """Run OCR/model reading for selected attachment pages only."""
    from ai_agent.purchase_invoice_ai import api as legacy

    run = frappe.get_doc("AI Run", run_name)
    legacy._assert_run_owner(run)
    selected_pages = frappe.parse_json(pages or "[]")
    if not isinstance(selected_pages, list) or not selected_pages:
        frappe.throw(_("Select at least one page to read."))

    normalized_pages = []
    already_read = 0
    session = legacy._session_for_run(run.name)
    for item in selected_pages:
        file_id = item.get("file_id")
        page_number = cint(item.get("page")) or 1
        if file_id:
            if legacy._session_page_already_read(session, file_id, page_number):
                already_read += 1
                continue
            normalized_pages.append({"file_id": file_id, "page": page_number})
    if not normalized_pages:
        legacy._finalize_page_reading_if_done(run.name)
        return {
            "session_name": session.name if session else None,
            "run_name": run.name,
            "status": "completed",
            "message": _("كل الصفحات المحددة مقروءة مسبقًا. لم أعد تشغيل OCR."),
        }
    if session:
        legacy._mark_session_pages_selected(session, normalized_pages)

    run.status = "Queued"
    run.cancel_requested = 0
    run.error_summary = None
    run.completed_at = None
    run.current_step = 1
    run.prompt = f"{run.prompt or ''}\nSelected pages for OCR: {json.dumps(normalized_pages, ensure_ascii=False)}"
    legacy._safe_save_run(run)
    frappe.db.set_value(
        "AI Run",
        run.name,
        {
            "status": "Queued",
            "cancel_requested": 0,
            "error_summary": None,
            "completed_at": None,
        },
        update_modified=False,
    )
    frappe.db.commit()

    legacy._start_background_page_reading(
        run.name,
        normalized_pages,
        frappe.session.user,
        getattr(frappe.local, "site", None),
    )
    return {
        "session_name": session.name if session else None,
        "run_name": run.name,
        "status": "queued",
        "message": _("بدأت قراءة الصفحات غير المقروءة فقط، وستظهر النتائج تدريجيًا.")
        if already_read
        else _("بدأت قراءة كل صفحة كعملية مستقلة، وستظهر النتائج تدريجيًا."),
    }


def get_session(session_name):
    from ai_agent.purchase_invoice_ai import api as legacy

    session = frappe.get_doc("Financial Attachment Session", session_name)
    legacy._assert_session_access(session)
    if session.source_run:
        legacy._finalize_page_reading_if_done(session.source_run)
        session.reload()
    analysis = None
    if session.source_run:
        run = frappe.get_doc("AI Run", session.source_run)
        legacy._assert_run_access(run)
        analysis = legacy._attachment_reading_from_session(run.name) or legacy._extract_json(run.final_answer or "")
    return {
        "session": legacy._session_payload(session),
        "analysis": analysis,
        "run_name": session.source_run,
    }


def set_selected_pages(session_name, pages):
    from ai_agent.purchase_invoice_ai import api as legacy

    session = frappe.get_doc("Financial Attachment Session", session_name)
    legacy._assert_session_owner(session)
    selected = frappe.parse_json(pages or "[]")
    selected_ids = {str(row.get("page_id") or "") for row in selected if row.get("page_id")}
    for row in session.pages:
        row.selected = 1 if row.page_id in selected_ids else 0
        if row.can_read and row.status in {"Ready", "Selected"}:
            row.status = "Selected" if row.selected else "Ready"
    legacy._safe_save_session(session)
    return {"session": legacy._session_payload(session)}


def get_page_preview(session_name, page_id):
    from ai_agent.purchase_invoice_ai import api as legacy

    session = frappe.get_doc("Financial Attachment Session", session_name)
    legacy._assert_session_access(session)
    page_row = next((row for row in session.pages if row.page_id == page_id), None)
    if not page_row:
        frappe.throw(_("Page was not found in this session."))
    frappe.db.set_value("Financial Attachment Session", session_name, "current_page_id", page_id, update_modified=False)
    file_doc = frappe.get_doc("File", page_row.file)
    service = FileContextService()
    extension = os.path.splitext(file_doc.file_name or file_doc.name)[1].lower()
    preview = ""
    if extension == ".pdf":
        try:
            import fitz

            document = fitz.open(get_file_path(file_doc.name))
            if 1 <= cint(page_row.page_number) <= len(document):
                preview = service._page_preview_data_url(document.load_page(cint(page_row.page_number) - 1))
            document.close()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Financial Attachment Page Preview Failed")
    elif extension in {".png", ".jpg", ".jpeg", ".webp"}:
        preview = service._image_preview_data_url(file_doc)
    return {
        "page_id": page_row.page_id,
        "preview_data_url": preview,
        "text_preview": page_row.text_preview or "",
        "status": page_row.status,
    }
