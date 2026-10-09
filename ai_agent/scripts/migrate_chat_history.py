"""One-off, run by hand when the owner asks (never a patch):

    bench --site <site> execute ai_agent.scripts.migrate_chat_history.execute

Turns every Desk conversation (AI Chat Session → chain of AI Run) into
chat messages with blocks, so old sessions open in the new workspace.

Per run, in creation order:
  user message      ← run.prompt (+ its File attachments re-pointed)
  assistant message ← final_answer / error_summary, with blocks from
                      AI Run Step + AI Tool Call (tool_use / tool_result),
                      AI Approval Request (approval), AI Artifact (artifact)
Thinking was never stored and cannot be recovered. Sessions whose runs came
from MCP, WhatsApp, the chatbot widget or Purchase Invoice AI are tagged
with that surface and stay out of the Desk sidebar.
"""

import json

import frappe
from frappe.utils import cint

APPROVAL_NOTE_MARKER = "✅ **تم تنفيذ الموافقة**"


def execute():
    if not frappe.db.exists("DocType", "AI Chat Message"):
        return
    support_user = None
    if frappe.db.exists("DocType", "AI WhatsApp Support Settings"):
        support_user = frappe.db.get_single_value("AI WhatsApp Support Settings", "support_user")

    sessions = frappe.get_all("AI Chat Session", fields=["name", "user", "surface", "last_seq"])
    for s in sessions:
        if cint(s.last_seq):
            continue   # already migrated (or created by the new API)
        runs = frappe.get_all(
            "AI Run",
            filters={"chat_session": s.name},
            fields=["name", "user", "prompt", "final_answer", "error_summary", "status", "creation", "completed_at", "modified",
                    "provider", "model", "context_doctype", "context_name", "input_tokens", "output_tokens",
                    "cache_read_input_tokens", "cache_creation_input_tokens", "tool_call_count", "current_step", "started_at"],
            order_by="creation asc",
        )
        if not runs:
            continue
        surface = _surface(runs, support_user)
        frappe.db.set_value("AI Chat Session", s.name, "surface", surface, update_modified=False)
        if surface != "Desk":
            frappe.db.set_value("AI Chat Session", s.name, "last_seq", 0, update_modified=False)
            continue
        seq = 0
        for run in runs:
            seq = _migrate_run(s, run, seq)
        frappe.db.set_value("AI Chat Session", s.name, {
            "last_seq": seq,
            "message_count": seq,
            "provider": runs[-1].provider,
            "model": runs[-1].model,
            "input_tokens": sum(cint(r.input_tokens) for r in runs),
            "output_tokens": sum(cint(r.output_tokens) for r in runs),
            "cache_read_input_tokens": sum(cint(r.cache_read_input_tokens) for r in runs),
            "cache_creation_input_tokens": sum(cint(r.cache_creation_input_tokens) for r in runs),
        }, update_modified=False)
        frappe.db.commit()


def _surface(runs, support_user):
    first = runs[0]
    if (first.prompt or "").startswith("MCP tool call:"):
        return "MCP"
    if support_user and first.user == support_user:
        return "WhatsApp"
    names = [r.name for r in runs]
    if frappe.db.exists("DocType", "AI WhatsApp Request") and frappe.db.exists("AI WhatsApp Request", {"ai_run": ("in", names)}):
        return "WhatsApp"
    if frappe.db.exists("DocType", "AI Chatbot Message") and frappe.db.exists("AI Chatbot Message", {"ai_run": ("in", names)}):
        return "Chatbot"
    if frappe.db.exists("DocType", "Financial Attachment Session") and (
        frappe.db.exists("Financial Attachment Session", {"source_run": ("in", names)})
        or frappe.db.exists("Financial Attachment Session", {"analysis_run": ("in", names)})
    ):
        return "API"
    return "Desk"


def _migrate_run(session, run, seq):
    status_map = {"Completed": "completed", "Failed": "failed", "Cancelled": "cancelled", "Timed Out": "failed", "Waiting Approval": "completed"}
    seq += 1
    user_msg = frappe.get_doc({
        "doctype": "AI Chat Message", "session": session.name, "seq": seq, "role": "user", "status": "completed",
        "user": run.user, "text": run.prompt or "", "context_doctype": run.context_doctype, "context_name": run.context_name,
        "completed_at": run.creation,
    }).insert(ignore_permissions=True)
    frappe.db.set_value("AI Chat Message", user_msg.name, "creation", run.creation, update_modified=False)
    frappe.db.sql(
        "update `tabFile` set attached_to_doctype='AI Chat Message', attached_to_name=%s where attached_to_doctype='AI Run' and attached_to_name=%s",
        (user_msg.name, run.name),
    )
    if user_msg.text:
        frappe.get_doc({"doctype": "AI Message Block", "message": user_msg.name, "seq": 0, "block_type": "text", "status": "completed", "content": user_msg.text}).insert(ignore_permissions=True)

    seq += 1
    text, note = _split_approval_note(run.final_answer or "")
    status = status_map.get(run.status, "completed" if text else "failed")
    assistant = frappe.get_doc({
        "doctype": "AI Chat Message", "session": session.name, "seq": seq, "role": "assistant", "status": status,
        "user": run.user, "run": run.name, "parent_message": user_msg.name, "provider": run.provider, "model": run.model,
        "text": text, "error": (run.error_summary or None) if status == "failed" else None,
        "input_tokens": cint(run.input_tokens), "output_tokens": cint(run.output_tokens),
        "cache_read_input_tokens": cint(run.cache_read_input_tokens), "cache_creation_input_tokens": cint(run.cache_creation_input_tokens),
        "steps": cint(run.current_step), "tool_calls": cint(run.tool_call_count),
        "started_at": run.started_at or run.creation, "completed_at": run.completed_at or run.modified,
    }).insert(ignore_permissions=True)
    frappe.db.set_value("AI Chat Message", assistant.name, "creation", run.creation, update_modified=False)
    frappe.db.set_value("AI Run", run.name, {"session": session.name, "message": assistant.name}, update_modified=False)

    bseq = 0
    steps = frappe.get_all("AI Run Step", filters={"run": run.name}, fields=["name", "sequence", "step_type", "title", "summary", "status", "started_at", "completed_at", "duration_ms"], order_by="sequence asc")
    calls = frappe.get_all("AI Tool Call", filters={"run": run.name}, fields=["name", "step", "tool_name", "arguments_json", "result_preview", "status", "started_at", "completed_at", "duration_ms"], order_by="creation asc")
    calls_by_step = {}
    for c in calls:
        calls_by_step.setdefault(c.step, []).append(c)
    unmatched = [c for c in calls if not c.step]
    for st in steps:
        if st.step_type != "Tool":
            continue
        step_calls = calls_by_step.get(st.name) or (unmatched[:1] if unmatched else [])
        if step_calls and step_calls[0] in unmatched:
            unmatched.remove(step_calls[0])
        for c in step_calls:
            call_id = f"legacy_{c.name}"
            try:
                args = json.loads(c.arguments_json or "{}")
            except ValueError:
                args = {"_raw": c.arguments_json}
            _block(assistant.name, bseq, "tool_use", tool_call_id=call_id, tool_name=(c.tool_name or "").replace(".", "__"), content_json=args, status="completed", started_at=c.started_at, completed_at=c.completed_at, tool_call=c.name)
            bseq += 1
            preview = c.result_preview or st.summary or ""
            _block(assistant.name, bseq, "tool_result", tool_call_id=call_id, tool_name=(c.tool_name or "").replace(".", "__"), content=preview, content_json=_preview_json(preview), status="failed" if c.status in ("Denied", "Failed") else "completed", started_at=c.started_at, completed_at=c.completed_at, duration_ms=c.duration_ms, tool_call=c.name)
            bseq += 1
        if not step_calls and st.summary:
            _block(assistant.name, bseq, "status", content=f"{st.title or ''}: {st.summary}".strip(": "), status="completed")
            bseq += 1

    for a in frappe.get_all("AI Approval Request", filters={"run": run.name}, fields=["name"], order_by="creation asc"):
        doc = frappe.get_doc("AI Approval Request", a.name)
        try:
            payload = json.loads(doc.payload_json or "{}")
        except ValueError:
            payload = {}
        snapshot = {
            "approval": doc.name, "approval_id": doc.approval_id, "status": doc.status, "action_type": doc.action_type,
            "doctype_name": doc.doctype_name, "document_name": doc.document_name, "action_summary": doc.action_summary,
            "impact_summary": doc.impact_summary, "payload": payload, "expires_at": str(doc.expires_at or ""),
            "requested_by": doc.requested_by, "decided_by": doc.approved_by, "decided_at": str(doc.decided_at or ""),
            "result": _json(doc.execution_result),
        }
        _block(assistant.name, bseq, "approval", content_json=snapshot, approval=doc.name, status="completed" if doc.status == "Executed" else ("denied" if doc.status == "Rejected" else "completed"))
        bseq += 1

    for art in frappe.get_all("AI Artifact", filters={"run": run.name}, fields=["name", "artifact_type", "title", "summary", "row_count", "is_truncated", "content_json"], order_by="sequence asc, creation asc"):
        content = _json(art.content_json) or {}
        rows = content.get("rows") or []
        data = {"artifact": art.name, "artifact_type": art.artifact_type, "title": art.title, "summary": art.summary, "row_count": art.row_count,
                "is_truncated": bool(art.is_truncated) or len(rows) > 50, "columns": content.get("columns") or (list(rows[0].keys()) if rows else []), "rows": rows[:50]}
        if art.artifact_type == "Chart":
            data["chart"] = content.get("chart") or content
        if art.artifact_type == "Document Links":
            data["links"] = content.get("links") or content.get("documents") or rows
        _block(assistant.name, bseq, "artifact", content=art.title or "", content_json=data, artifact=art.name, tool_name=art.artifact_type)
        bseq += 1

    if text:
        _block(assistant.name, bseq, "text", content=text)
        bseq += 1
    if note:
        _block(assistant.name, bseq, "status", content=note)
        bseq += 1
    if status == "failed" and run.error_summary:
        _block(assistant.name, bseq, "error", content=run.error_summary, status="failed")
        bseq += 1
    return seq


def _block(message, seq, block_type, content="", content_json=None, status="completed", **extra):
    doc = {"doctype": "AI Message Block", "message": message, "seq": seq, "block_type": block_type, "status": status, "content": content or ""}
    if content_json is not None:
        doc["content_json"] = json.dumps(content_json, ensure_ascii=False, default=str)
    for key, value in extra.items():
        if value not in (None, ""):
            doc[key] = value
    frappe.get_doc(doc).insert(ignore_permissions=True)


def _split_approval_note(answer):
    if APPROVAL_NOTE_MARKER not in answer:
        return answer.strip(), ""
    head, _, tail = answer.partition(APPROVAL_NOTE_MARKER)
    head = head.rstrip().rstrip("-").rstrip()
    return head.strip(), (APPROVAL_NOTE_MARKER + tail).strip()


def _json(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _preview_json(preview):
    data = _json(preview)
    return data if isinstance(data, dict) else ({"result": data} if data is not None else None)
