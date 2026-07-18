import json
import hashlib
import frappe
from frappe import _
from ai_agent.runtime.constants import REALTIME_EVENTS
from ai_agent.runtime.exceptions import (
    ApprovalRequired, ApprovalExpired, ApprovalAlreadyExecuted,
    PermissionDenied, IdempotencyConflict, ConcurrencyConflict,
)


class ApprovalEngine:
    """Manages the approval lifecycle for write operations."""

    def __init__(self, permission_context):
        self.ctx = permission_context

    def create_approval_request(self, *, run_name, action_type, doctype_name=None,
                                 document_name=None, action_summary, impact_summary="",
                                 payload, expected_modified=None, idempotency_key=None):
        """Create an approval request and pause the run."""
        if idempotency_key:
            existing = frappe.db.exists("AI Approval Request", {"idempotency_key": idempotency_key})
            if existing:
                existing_doc = frappe.get_doc("AI Approval Request", existing)
                if existing_doc.status == "Executed":
                    return existing_doc
                raise IdempotencyConflict("An approval request with this key already exists.")

        settings = frappe.get_doc("AI Settings")
        ttl = settings.default_approval_ttl_minutes or 30

        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

        now = frappe.utils.now_datetime()
        expires_at = frappe.utils.add_to_date(now, minutes=ttl)

        approval = frappe.get_doc({
            "doctype": "AI Approval Request",
            "run": run_name,
            "approval_id": f"APR-{frappe.utils.cint(now.timestamp())}",
            "action_type": action_type,
            "doctype_name": doctype_name,
            "document_name": document_name,
            "action_summary": action_summary,
            "impact_summary": impact_summary,
            "payload_json": payload_json,
            "payload_hash": payload_hash,
            "expected_modified": expected_modified,
            "status": "Pending",
            "requested_by": frappe.session.user,
            "requested_at": now,
            "expires_at": expires_at,
            "idempotency_key": idempotency_key or "",
        })
        approval.insert(ignore_permissions=True)
        frappe.db.commit()

        frappe.publish_realtime(
            REALTIME_EVENTS["approval_required"],
            {"approval_id": approval.name, "run_id": run_name, "action_summary": action_summary},
            user=frappe.session.user,
        )

        return approval

    def approve(self, approval_name, user=None):
        """Approve an approval request."""
        user = user or frappe.session.user
        if user == "Guest":
            raise PermissionDenied("Guest users cannot approve actions.")

        approval = frappe.get_doc("AI Approval Request", approval_name)

        if approval.status != "Pending":
            raise ApprovalAlreadyExecuted(f"Approval is already {approval.status}.")

        if approval.expires_at and frappe.utils.now_datetime() > approval.expires_at:
            approval.status = "Expired"
            approval.save(ignore_permissions=True)
            frappe.db.commit()
            raise ApprovalExpired("Approval request has expired.")

        if approval.payload_hash != self._compute_hash(approval.payload_json):
            raise PermissionDenied("Payload has been tampered with.")

        if approval.requested_by != user and "System Manager" not in frappe.get_roles(user):
            raise PermissionDenied("You are not authorized to approve this action.")

        approval.status = "Approved"
        approval.approved_by = user
        approval.decided_at = frappe.utils.now_datetime()
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return approval

    def reject(self, approval_name, reason=None, user=None):
        """Reject an approval request."""
        user = user or frappe.session.user
        approval = frappe.get_doc("AI Approval Request", approval_name)

        if approval.status != "Pending":
            raise ApprovalAlreadyExecuted(f"Approval is already {approval.status}.")

        approval.status = "Rejected"
        approval.approved_by = user
        approval.decided_at = frappe.utils.now_datetime()
        if reason:
            approval.impact_summary = f"{approval.impact_summary}\n\nRejection reason: {reason}"
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return approval

    def execute_approved_action(self, approval_name):
        """Execute an approved action."""
        approval = frappe.get_doc("AI Approval Request", approval_name)

        if approval.status != "Approved":
            raise ApprovalAlreadyExecuted(f"Cannot execute: approval status is {approval.status}.")

        if approval.expires_at and frappe.utils.now_datetime() > approval.expires_at:
            approval.status = "Expired"
            approval.save(ignore_permissions=True)
            frappe.db.commit()
            raise ApprovalExpired("Approval has expired before execution.")

        user = approval.approved_by
        if approval.action_type == "Bulk Create":
            payload = json.loads(approval.payload_json or "{}")
            self._assert_bulk_create_permission(payload, user)
        elif not frappe.has_permission(approval.doctype_name, "create", user=user):
            raise PermissionDenied(f"User {user} no longer has create permission on {approval.doctype_name}.")

        return self._execute_by_type(approval)

    def _execute_by_type(self, approval):
        """Route execution based on action type."""
        payload = json.loads(approval.payload_json)

        if approval.action_type == "Create":
            return self._execute_create(approval, payload)
        elif approval.action_type == "Bulk Create":
            return self._execute_bulk_create(approval, payload)
        elif approval.action_type == "Update":
            return self._execute_update(approval, payload)
        elif approval.action_type == "Submit":
            return self._execute_submit(approval)
        elif approval.action_type == "Cancel":
            return self._execute_cancel(approval)
        else:
            raise ApprovalRequired(f"Action type '{approval.action_type}' not yet implemented.")

    def _execute_create(self, approval, payload):
        """Execute a create draft action."""
        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=False)
        frappe.db.commit()

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"document_name": doc.name, "doctype": approval.doctype_name}

    def _execute_bulk_create(self, approval, payload):
        """Execute multiple create draft actions in one transaction."""
        documents = self._bulk_documents(payload)
        created = []

        try:
            for document in documents:
                doc = frappe.get_doc(document["payload"])
                doc.insert(ignore_permissions=False)
                created.append({"doctype": doc.doctype, "document_name": doc.name})
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            approval.status = "Failed"
            approval.executed_at = frappe.utils.now_datetime()
            approval.execution_result = json.dumps(
                {
                    "error": frappe.get_traceback(),
                    "created_before_rollback": created,
                    "rolled_back": True,
                },
                default=str,
            )
            approval.save(ignore_permissions=True)
            frappe.db.commit()
            raise

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"documents": created, "count": len(created)}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"documents": created, "count": len(created), "doctype": approval.doctype_name}

    def _execute_update(self, approval, payload):
        """Execute an update action."""
        doc_name = approval.document_name
        doc = frappe.get_doc(approval.doctype_name, doc_name)

        if approval.expected_modified and str(doc.modified) != str(approval.expected_modified):
            raise ConcurrencyConflict("Document was modified since the approval was requested.")

        for key, val in payload.items():
            if key != "name" and key != "doctype":
                doc.set(key, val)

        doc.save(ignore_permissions=False)
        frappe.db.commit()

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"document_name": doc.name, "doctype": approval.doctype_name}

    def _execute_submit(self, approval):
        """Execute a submit action."""
        doc = frappe.get_doc(approval.doctype_name, approval.document_name)
        doc.submit()
        frappe.db.commit()

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name, "docstatus": 1}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"document_name": doc.name, "doctype": approval.doctype_name, "docstatus": 1}

    def _execute_cancel(self, approval):
        """Execute a cancel action."""
        doc = frappe.get_doc(approval.doctype_name, approval.document_name)
        doc.cancel()
        frappe.db.commit()

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name, "docstatus": 2}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"document_name": doc.name, "doctype": approval.doctype_name, "docstatus": 2}

    def _compute_hash(self, payload_json):
        return hashlib.sha256(payload_json.encode()).hexdigest()

    def _assert_bulk_create_permission(self, payload, user):
        for document in self._bulk_documents(payload):
            doc_type = document["doctype"]
            if not frappe.has_permission(doc_type, "create", user=user):
                raise PermissionDenied(f"User {user} no longer has create permission on {doc_type}.")

    def _bulk_documents(self, payload):
        documents = payload.get("documents") if isinstance(payload, dict) else None
        if not isinstance(documents, list) or not documents:
            raise ApprovalRequired("Bulk Create payload must include documents.")

        normalized = []
        for document in documents:
            if not isinstance(document, dict):
                raise ApprovalRequired("Each bulk document must be an object.")
            payload = dict(document.get("payload") or {})
            doc_type = document.get("doctype") or payload.get("doctype")
            if not doc_type or not payload:
                raise ApprovalRequired("Each bulk document must include doctype and payload.")
            payload["doctype"] = doc_type
            normalized.append({"doctype": doc_type, "payload": payload})
        return normalized

    def expire_pending(self):
        """Expire all pending approvals past their TTL."""
        now = frappe.utils.now_datetime()
        pending = frappe.get_all(
            "AI Approval Request",
            filters={"status": "Pending", "expires_at": ("<", now)},
            pluck="name",
        )
        for name in pending:
            frappe.db.set("AI Approval Request", name, "status", "Expired", update_modified=False)
        if pending:
            frappe.db.commit()
        return len(pending)


def expire_pending_approvals():
    """Scheduled task to expire pending approvals."""
    from ai_agent.runtime.permission_context import PermissionContext
    engine = ApprovalEngine(PermissionContext("Administrator"))
    return engine.expire_pending()
