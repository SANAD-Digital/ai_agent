"""Approval decisions inside a chat message.

The assistant message stopped at an approval block. A decision executes or
rejects through the existing ApprovalEngine, updates the block, appends the
tool_result the model is waiting for, and resumes the same message.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import now_datetime

from ai_agent.engine import runner
from ai_agent.engine.tools import approval_snapshot
from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.permission_context import PermissionContext
from ai_agent.runtime.untrusted import wrap


def decide(approval_name, decision, reason=None, user=None):
    """Returns (snapshot, message_name). Raises frappe exceptions on denial."""
    user = user or frappe.session.user
    block = frappe.db.get_value(
        "AI Message Block", {"approval": approval_name, "block_type": "approval"},
        ["name", "message", "seq", "tool_call_id", "tool_name"], as_dict=True,
    )
    outcome = ApprovalEngine(PermissionContext(user)).decide(approval_name, decision, user=user, reason=reason)
    result, error = outcome.result, (outcome.error or "")[:500] or None

    approval = frappe.get_doc("AI Approval Request", approval_name)
    snapshot = approval_snapshot(approval)
    snapshot.update({
        "decided_by": user,
        "decided_at": str(now_datetime()),
        "reason": reason or "",
        "result": _jsonable(result) if result is not None else (_execution_result(approval) if approval.status == "Executed" else None),
        "error": error,
    })

    if not block:
        return snapshot, None

    frappe.db.set_value("AI Message Block", block.name, {
        "content_json": json.dumps(snapshot, ensure_ascii=False, default=str),
        "status": "completed" if approval.status == "Executed" else ("denied" if approval.status == "Rejected" else "failed"),
        "completed_at": now_datetime(),
    }, update_modified=False)

    message = frappe.get_doc("AI Chat Message", block.message)
    session_owner = message.user
    pub = runner.Publisher(message.session, message.name, session_owner, start_seq=frappe.utils.cint(message.event_seq))
    pub.publish(_ApprovalDecided(block.seq, _block_dict(block, snapshot)))

    # The model's pending tool call gets its result, then the message resumes.
    if message.status == "awaiting_approval":
        outcome = {
            "approval": approval.name,
            "status": approval.status,
            "decision": decision,
            "result": snapshot["result"],
            "error": error,
            "reason": reason or None,
        }
        if approval.status == "Executed":
            outcome["note"] = "The user approved and the action was executed. Tell the user what was created or changed (link the document) and suggest the natural next step."
        elif approval.status == "Rejected":
            outcome["note"] = "The user rejected this action. Do not retry it. Ask what they would like to change, if anything."
        else:
            outcome["note"] = "The action failed after approval. Explain the error to the user and propose a fix."
        last = frappe.get_all("AI Message Block", filters={"message": message.name}, fields=["seq"], order_by="seq desc", limit=1)
        next_seq = (last[0].seq if last else -1) + 1
        result_block = frappe.get_doc({
            "doctype": "AI Message Block",
            "message": message.name,
            "seq": next_seq,
            "block_type": "tool_result",
            "status": "completed" if not error and approval.status != "Rejected" else "failed",
            "tool_call_id": block.tool_call_id,
            "tool_name": block.tool_name,
            "is_user_visible": 0,
            "started_at": now_datetime(),
            "completed_at": now_datetime(),
        }).insert(ignore_permissions=True)
        frappe.db.set_value("AI Message Block", result_block.name, {
            "content": wrap(json.dumps(outcome, ensure_ascii=False, default=str), f"tool {block.tool_name or 'actions'}"),
            "content_json": json.dumps({"approval": approval.name, "status": approval.status, "result": snapshot["result"], "error": error}, ensure_ascii=False, default=str),
        }, update_modified=False)
        message.db_set({"status": "pending", "event_seq": pub.seq}, update_modified=False)
        if message.run:
            frappe.db.set_value("AI Run", message.run, {"status": "Resuming", "cancel_requested": 0}, update_modified=False)
        frappe.db.commit()
        runner.enqueue(message.name, session_owner, resume=True)
    else:
        message.db_set({"event_seq": pub.seq}, update_modified=False)
        frappe.db.commit()
    return snapshot, message.name


def _execution_result(approval):
    try:
        return json.loads(approval.execution_result or "null")
    except ValueError:
        return {"raw": approval.execution_result}


def _jsonable(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return {"raw": str(value)}


def _block_dict(block, snapshot):
    return {"seq": block.seq, "kind": "approval", "status": "completed", "data": snapshot, "id": block.tool_call_id, "name": block.tool_name}


class _ApprovalDecided:
    def __init__(self, seq, block):
        self.seq, self.block = seq, block

    def to_dict(self):
        return {"type": "approval.decided", "block_seq": self.seq, "block": self.block}


def settle_waiting(message_name, stop_reason):
    """Close a message that stopped at an approval without waiting for it any longer: its approval expired, or the
    user went on with a new message. The card stays; a later decision executes it without resuming this reply
    (a resumed reply would land above the newer turns)."""
    msg = frappe.db.get_value("AI Chat Message", message_name, ["name", "status", "run", "session", "user", "event_seq"], as_dict=True)
    if not msg or msg.status != "awaiting_approval":
        return False
    frappe.db.set_value("AI Chat Message", msg.name, {"status": "completed", "stop_reason": stop_reason, "completed_at": now_datetime()}, update_modified=False)
    if msg.run:
        frappe.db.set_value("AI Run", msg.run, {"status": "Completed", "completed_at": now_datetime()}, update_modified=False)
    from ai_agent.engine.events import MessageDone

    pub = runner.Publisher(msg.session, msg.name, msg.user, start_seq=frappe.utils.cint(msg.event_seq))
    pub.publish(MessageDone(status="completed", stop_reason=stop_reason))
    frappe.db.set_value("AI Chat Message", msg.name, "event_seq", pub.seq, update_modified=False)
    return True


def on_approval_expired(approval_name):
    """The expiry job: refresh the card (no live Approve button on an expired request) and settle its message."""
    block = frappe.db.get_value("AI Message Block", {"approval": approval_name, "block_type": "approval"}, ["name", "message", "content_json"], as_dict=True)
    if not block:
        return
    approval = frappe.get_doc("AI Approval Request", approval_name)
    snapshot = approval_snapshot(approval)
    frappe.db.set_value("AI Message Block", block.name, {"content_json": json.dumps(snapshot, ensure_ascii=False, default=str), "status": "failed"}, update_modified=False)
    settle_waiting(block.message, "approval_expired")
