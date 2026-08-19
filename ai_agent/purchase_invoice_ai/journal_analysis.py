import json

import frappe
from frappe import _
from frappe.utils import cint


def _has_provider_timeout(errors):
    text = "\n".join(str(error or "") for error in errors or []).lower()
    return "read timed out" in text or "timed out" in text or "timeout" in text


def start_analysis_from_review(source_run_name, analysis, user_instructions="", defaults=None):
    """Start direct chunk analysis from saved OCR/review data without queue workers."""
    from ai_agent.purchase_invoice_ai import api as legacy

    source_run = frappe.get_doc("AI Run", source_run_name)
    legacy._assert_run_access(source_run)
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot create runs."))
    legacy._assert_ai_enabled()
    legacy._assert_read("Company")
    legacy._ensure_purchase_invoice_agent()

    reviewed = frappe.parse_json(analysis or "{}")
    defaults = frappe.parse_json(defaults or "{}")
    reviewed = legacy._prefer_latest_source_reading(source_run, reviewed)
    if not legacy._analysis_groups(reviewed):
        frappe.throw(_("No extracted invoice data to analyze."))
    unread = legacy._session_unread_page_summary(source_run.name)
    if unread.get("unread_count"):
        frappe.throw(unread.get("message"))

    return create_direct_analysis_run(
        source_run=source_run,
        source_payload=reviewed,
        previous_run_name="",
        user_instructions=user_instructions,
        defaults=defaults,
    )


def reanalyze_from_saved_ocr(run_name, user_instructions="", defaults=None):
    """Start a fresh direct analysis run from the latest saved OCR without rereading files."""
    from ai_agent.purchase_invoice_ai import api as legacy

    run = frappe.get_doc("AI Run", run_name)
    legacy._assert_run_access(run)
    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot create runs."))
    legacy._assert_ai_enabled()
    legacy._assert_read("Company")
    legacy._ensure_purchase_invoice_agent()

    source_run = frappe.get_doc("AI Run", run.parent_run) if run.parent_run else run
    legacy._assert_run_access(source_run)
    reviewed = legacy._attachment_reading_from_session(source_run.name) or legacy._attachment_reading_from_run(
        source_run,
        recovery_warning=False,
    )
    if not legacy._analysis_groups(reviewed):
        frappe.throw(_("لا توجد قراءة OCR محفوظة لإعادة التحليل. اقرأ الصفحات أولًا."))
    unread = legacy._session_unread_page_summary(source_run.name)
    if unread.get("unread_count"):
        frappe.throw(unread.get("message"))

    defaults = frappe.parse_json(defaults or "{}")
    if not isinstance(defaults, dict):
        defaults = {}
    defaults["_reanalyze_at"] = frappe.utils.now()
    defaults["_reanalyze_from"] = run.name

    return create_direct_analysis_run(
        source_run=source_run,
        source_payload=reviewed,
        previous_run_name=run.name,
        user_instructions=user_instructions,
        defaults=defaults,
    )


def create_direct_analysis_run(source_run, source_payload, previous_run_name="", user_instructions="", defaults=None):
    from ai_agent.purchase_invoice_ai import api as legacy

    defaults = defaults if isinstance(defaults, dict) else {}
    pages = legacy._read_pages_for_model_analysis(source_payload)
    if not pages and not legacy._analysis_groups(source_payload):
        frappe.throw(_("لا توجد قراءة OCR محفوظة لإعادة التحليل. اقرأ الصفحات أولًا."))

    cancel_prior_analysis_runs(source_run.name, exclude_run=previous_run_name)
    prompt_text = _build_agent_workspace_processing_prompt(
        company=source_run.company,
        source_run_name=source_run.name,
        source_payload=source_payload,
        pages=pages,
        previous_run_name=previous_run_name,
        user_instructions=user_instructions,
        defaults=defaults,
    )
    retry_key = legacy._review_retry_key(source_run.name, source_payload, user_instructions, defaults)
    existing = frappe.db.exists("AI Run", {"idempotency_key": retry_key})
    if existing:
        run_name = existing
        status = "already_exists"
    else:
        new_run = frappe.get_doc({
            "doctype": "AI Run",
            "agent": legacy.PURCHASE_AGENT,
            "user": frappe.session.user,
            "company": source_run.company,
            "prompt": prompt_text,
            "status": "Queued",
            "parent_run": source_run.name,
            "started_at": frappe.utils.now_datetime(),
            "idempotency_key": retry_key,
        })
        new_run.final_answer = json.dumps(_initial_page_pipeline_payload(source_run.name, len(pages)), ensure_ascii=False, default=str)
        new_run.insert(ignore_permissions=True)
        _link_session_analysis_run(source_run.name, new_run.name)
        frappe.db.commit()
        legacy._start_background_run(new_run.name, frappe.session.user, getattr(frappe.local, "site", None))
        run_name = new_run.name
        status = "queued"
    current_analysis = {}
    if run_name:
        run = frappe.get_doc("AI Run", run_name)
        legacy._assert_run_access(run)
        current_analysis = legacy._extract_json(run.final_answer or "") or {}
    return {
        "run_name": run_name,
        "source_run_name": source_run.name,
        "status": status,
        "files": [],
        "analysis": current_analysis or legacy._canonicalize_journal_payload(source_payload, allow_page_fallback=False),
        "message": _("بدأت معالجة الجلسة على مستوى الصفحات ثم التجميع النهائي للقيد."),
    }


def _initial_page_pipeline_payload(source_run_name, page_count):
    return {
        "invoice_groups": [],
        "questions": [],
        "warnings": [],
        "errors": [],
        "workflow": {
            "stage": "page_level_agent_processing",
            "mode": "page_pipeline",
            "source_run": source_run_name,
            "is_partial": 1,
            "progress": {
                "total_chunks": cint(page_count),
                "completed_chunks": 0,
                "failed_chunks": 0,
                "percent": 0,
            },
        },
    }


def _link_session_analysis_run(source_run_name, analysis_run_name):
    try:
        session = frappe.db.get_value("Financial Attachment Session", {"source_run": source_run_name}, "name")
        if session:
            frappe.db.set_value("Financial Attachment Session", session, "analysis_run", analysis_run_name, update_modified=False)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Financial Attachment Session Analysis Link Failed")


def _build_agent_workspace_processing_prompt(
    *,
    company,
    source_run_name,
    source_payload,
    pages,
    previous_run_name="",
    user_instructions="",
    defaults=None,
):

    defaults = defaults if isinstance(defaults, dict) else {}
    prompt_parts = [
        "FINANCIAL_ATTACHMENT_PAGE_PIPELINE",
        "Financial Attachments AI processing task.",
        "",
        f"Company: {company or ''}",
        f"Source extraction run: {source_run_name or ''}",
        f"Previous processing run: {previous_run_name or ''}",
        f"User instructions: {user_instructions or 'None'}",
        f"Defaults JSON: {json.dumps(defaults, ensure_ascii=False, default=str)}",
        f"Readable page count: {len(pages or [])}",
        "The worker will process readable pages independently, persist page results, then run a final consolidation pass.",
        "Use the granted financial-attachments-to-journal-entry skill as the source of extraction and accounting behavior.",
    ]
    return "\n".join(prompt_parts)


def process_next_analysis_chunk(run_name):
    """Process one model chunk synchronously and return current partial result."""
    from ai_agent.purchase_invoice_ai import api as legacy

    run = frappe.get_doc("AI Run", run_name)
    legacy._assert_run_access(run)
    if run.status in legacy.TERMINAL_STATUSES:
        parsed = legacy._extract_json(run.final_answer or "") or {}
        return {"run": run_response(run), "analysis": parsed, "done": True}

    source_run_name = run.parent_run or run.name
    source_payload = legacy._attachment_reading_from_session(source_run_name)
    if not source_payload:
        source_run = frappe.get_doc("AI Run", source_run_name)
        source_payload = legacy._extract_json(source_run.final_answer or "") or legacy._attachment_reading_from_run(
            source_run,
            recovery_warning=False,
        )

    page_chunks = legacy._chunk_pages_for_model(legacy._read_pages_for_model_analysis(source_payload))
    if not page_chunks:
        failed = legacy._analysis_failed_payload(_("لا توجد قراءة OCR محفوظة للمعالجة."))
        run.status = "Failed"
        run.error_summary = legacy._first_analysis_error(failed)
        run.final_answer = json.dumps(failed, ensure_ascii=False, default=str)
        run.completed_at = frappe.utils.now_datetime()
        legacy._safe_save_run(run)
        return {"run": run_response(run), "analysis": failed, "done": True}

    current = legacy._extract_json(run.final_answer or "") or {"invoice_groups": []}
    progress = (current.get("workflow") or {}).get("progress") or {}
    completed = cint(progress.get("completed_chunks"))
    failed_chunks = cint(progress.get("failed_chunks"))
    next_index = completed + 1
    if next_index > len(page_chunks):
        run.status = "Completed" if legacy._analysis_has_extractable_data(current) else "Failed"
        run.completed_at = frappe.utils.now_datetime()
        if run.status == "Failed":
            run.error_summary = _("انتهت المعالجة بدون سطور قيد.")
        legacy._safe_save_run(run)
        return {"run": run_response(run), "analysis": current, "done": True}

    result = legacy._process_page_chunk_with_ai(
        company=run.company,
        compact_ctx={},
        page_chunk=page_chunks[next_index - 1],
        user_instructions=legacy._prompt_user_instructions(run.prompt),
        chunk_index=next_index,
        total_chunks=len(page_chunks),
    )
    chunk_results = []
    if legacy._analysis_groups(current):
        chunk_results.append(current)
    chunk_errors = list(current.get("errors") or []) if isinstance(current, dict) else []
    if isinstance(result, dict) and (result.get("workflow") or {}).get("analysis_failed"):
        failed_chunks += 1
        chunk_errors.extend([str(value) for value in result.get("errors") or []])
    elif result:
        chunk_results.append(result)

    updated = legacy._prepare_incremental_analysis(
        run.company,
        chunk_results,
        total_chunks=len(page_chunks),
        completed_chunks=next_index,
        failed_chunks=failed_chunks,
        errors=chunk_errors,
    )
    if (
        isinstance(result, dict)
        and (result.get("workflow") or {}).get("analysis_failed")
        and not chunk_results
    ):
        updated = result
        workflow = updated.setdefault("workflow", {})
        workflow.update({
            "stage": "model_page_chunk_analysis",
            "is_partial": next_index < len(page_chunks),
            "progress": {
                "total_chunks": len(page_chunks),
                "completed_chunks": next_index,
                "failed_chunks": failed_chunks,
                "percent": round((next_index / max(len(page_chunks), 1)) * 100, 2),
            },
            "failed_chunks": failed_chunks,
        })
    stop_for_provider_timeout = (
        failed_chunks >= 2
        and not legacy._analysis_groups(updated)
        and _has_provider_timeout(chunk_errors)
    )
    if stop_for_provider_timeout:
        updated.setdefault("workflow", {})["analysis_failed"] = 1
        updated.setdefault("workflow", {})["provider_unavailable"] = 1
        updated.setdefault("errors", []).append(
            _("تم إيقاف التحليل بعد فشل متكرر من مزود الموديل. لم يتم إعادة OCR؛ أعد التحليل بعد استقرار المزود أو زد مهلة الطلب.")
        )
    done = stop_for_provider_timeout or next_index >= len(page_chunks)
    run.status = (
        "Completed"
        if done
        and legacy._analysis_has_extractable_data(updated)
        and not (updated.get("workflow") or {}).get("analysis_failed")
        else "Running"
    )
    if done and run.status != "Completed":
        run.status = "Failed"
        run.error_summary = legacy._first_analysis_error(updated)
    if done:
        run.completed_at = frappe.utils.now_datetime()
        updated.setdefault("workflow", {})["is_partial"] = 0
    run.final_answer = json.dumps(updated, ensure_ascii=False, default=str)
    run.tool_call_count = next_index
    legacy._safe_save_run(run)
    return {"run": run_response(run), "analysis": updated, "done": done}


def prepare_journal_entry_analysis(company, analysis):
    """Return the model-produced Journal Entry analysis without local guessing."""
    from ai_agent.purchase_invoice_ai import api as legacy

    if frappe.session.user == "Guest":
        frappe.throw(_("Guest users cannot prepare Journal Entry lines."))
    legacy._assert_ai_enabled()
    legacy._ensure_purchase_invoice_agent()

    payload = frappe.parse_json(analysis or "{}")
    if not isinstance(payload, dict):
        frappe.throw(_("Invalid analysis payload."))

    payload = legacy._canonicalize_journal_payload(payload, allow_page_fallback=False)
    payload = legacy._compact_payload_text_fields(payload)
    return {"analysis": payload, "warnings": payload.get("warnings") or [], "status": "completed"}


def cancel_prior_analysis_runs(source_run_name, exclude_run=None):
    from ai_agent.purchase_invoice_ai import api as legacy

    names = frappe.get_all(
        "AI Run",
        filters={
            "parent_run": source_run_name,
            "agent": legacy.PURCHASE_AGENT,
            "status": ["in", ["Queued", "Running", "Planning"]],
        },
        pluck="name",
    )
    for name in names:
        if name == exclude_run:
            continue
        frappe.db.set_value("AI Run", name, {
            "status": "Cancelled",
            "error_summary": _("تم إلغاء محاولة تحليل سابقة بعد بدء إعادة تحليل أحدث من نفس OCR المحفوظ."),
            "completed_at": frappe.utils.now_datetime(),
        })
    if names:
        frappe.db.commit()


def run_response(run):
    return {
        "name": run.name,
        "status": run.status,
        "error_summary": run.error_summary,
        "current_step": run.current_step,
        "tool_call_count": run.tool_call_count,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
    }
