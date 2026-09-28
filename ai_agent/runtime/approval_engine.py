import json
import hashlib
import frappe
from ai_agent.runtime.constants import REALTIME_EVENTS
from ai_agent.runtime.exceptions import (
    ApprovalRequired, ApprovalExpired, ApprovalAlreadyExecuted,
    PermissionDenied, IdempotencyConflict, ConcurrencyConflict,
)
from ai_agent.runtime.write_guard import blocked_fields

PERMTYPE_BY_ACTION = {
    "Create": "create",
    "Update": "write",
    "Submit": "submit",
    "Cancel": "cancel",
    "Workflow": "write",
}
TARGET_NOT_ACCESSIBLE = "Target document not found or not accessible."


class ApprovalEngine:
    """Manages the approval lifecycle for write operations."""

    def __init__(self, permission_context):
        self.ctx = permission_context

    def create_approval_request(self, *, run_name, action_type, doctype_name=None,
                                 document_name=None, action_summary, impact_summary="",
                                 payload, expected_modified=None, idempotency_key=None):
        """Create an approval request and pause the run.

        Every write path (tools, action interceptor, MCP) comes through here,
        so the requester's permissions and the payload fields are checked
        before anything is queued for approval.
        """
        target = self.assert_action_allowed(
            action_type, doctype_name, document_name, payload, self.ctx.user
        )
        if action_type == "Update" and not expected_modified and target is not None:
            expected_modified = str(target.modified)

        if idempotency_key:
            existing = frappe.db.exists("AI Approval Request", {"idempotency_key": idempotency_key})
            if existing:
                existing_doc = frappe.get_doc("AI Approval Request", existing)
                if existing_doc.status == "Executed":
                    return existing_doc
                raise IdempotencyConflict("An approval request with this key already exists.")

        settings = frappe.get_cached_doc("AI Settings")
        ttl = settings.default_approval_ttl_minutes or 30

        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

        now = frappe.utils.now_datetime()
        expires_at = frappe.utils.add_to_date(now, minutes=ttl)

        approval = frappe.get_doc({
            "doctype": "AI Approval Request",
            "run": run_name,
            # Seconds alone collide when two requests land in the same second.
            "approval_id": f"APR-{frappe.utils.cint(now.timestamp())}-{frappe.generate_hash(length=6)}",
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

        settings = frappe.get_cached_doc("AI Settings")
        if settings.get("require_four_eyes") and approval.requested_by == user:
            raise PermissionDenied(
                "Four-eyes approval is enabled: the requester cannot approve their own action."
            )

        if approval.requested_by != user:
            roles = set(frappe.get_roles(user))
            approver_role = settings.get("approver_role")
            if "System Manager" not in roles and (not approver_role or approver_role not in roles):
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

        # Requester, run owner or an approver; anyone else could veto other users' work.
        if not self._can_decide(approval, user):
            raise PermissionDenied("You are not authorized to reject this action.")

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

        # Re-check against the approver (who executes it) at execution time:
        # permissions or the target document may have changed since the request.
        user = approval.approved_by
        self.assert_action_allowed(
            approval.action_type,
            approval.doctype_name,
            approval.document_name,
            json.loads(approval.payload_json or "{}"),
            user,
        )

        result = self._execute_by_type(approval)
        try:
            from ai_agent.runtime.memory_learning import learn_from_executed_approval

            learn_from_executed_approval(approval)
        except Exception:
            # Learning is best-effort: it must never fail an executed action.
            frappe.log_error(
                frappe.get_traceback(), f"AI Memory Learning Failed: {approval.name}"
            )
        return result

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
        elif approval.action_type == "Workflow":
            return self._execute_workflow(approval, payload)
        else:
            raise ApprovalRequired(f"Action type '{approval.action_type}' not yet implemented.")

    def _execute_create(self, approval, payload):
        """Execute a create draft action.

        The document write and the approval bookkeeping share one commit so a
        crash cannot leave an executed document behind a still-Approved
        request (which would invite a duplicate re-execution).
        """
        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=False)

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
            doc.set(key, val)

        doc.save(ignore_permissions=False)

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

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name, "docstatus": 2}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"document_name": doc.name, "doctype": approval.doctype_name, "docstatus": 2}

    def _execute_workflow(self, approval, payload):
        """Apply an approved workflow transition (transition permissions are
        enforced by apply_workflow against the approver's session roles)."""
        from frappe.model.workflow import apply_workflow

        doc = frappe.get_doc(approval.doctype_name, approval.document_name)
        updated = apply_workflow(doc, payload.get("action"))

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps(
            {
                "document_name": updated.name,
                "workflow_state": updated.get("workflow_state"),
            },
            default=str,
        )
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {
            "document_name": updated.name,
            "doctype": approval.doctype_name,
            "workflow_state": updated.get("workflow_state"),
        }

    def _can_decide(self, approval, user):
        if user == "Guest":
            return False
        if approval.requested_by == user:
            return True
        if approval.run and frappe.db.get_value("AI Run", approval.run, "user") == user:
            return True
        roles = set(frappe.get_roles(user))
        approver_role = frappe.get_cached_doc("AI Settings").get("approver_role")
        return "System Manager" in roles or bool(approver_role and approver_role in roles)

    def _compute_hash(self, payload_json):
        return hashlib.sha256(payload_json.encode()).hexdigest()

    def assert_action_allowed(self, action_type, doctype_name, document_name, payload, user):
        """Raise PermissionDenied unless ``user`` may perform this action.

        Returns the loaded target document for document-level actions.
        """
        if action_type == "Bulk Create":
            for document in self._bulk_documents(payload):
                self._assert_create_allowed(document["doctype"], document["payload"], user)
            return None
        if action_type == "Create":
            self._assert_create_allowed(doctype_name, payload, user)
            return None

        permtype = PERMTYPE_BY_ACTION.get(action_type)
        if not permtype:
            raise PermissionDenied(f"Action type '{action_type}' is not allowed.")
        if not doctype_name or not document_name or not frappe.db.exists("DocType", doctype_name):
            raise PermissionDenied(TARGET_NOT_ACCESSIBLE)
        if not frappe.has_permission(doctype_name, permtype, user=user):
            raise PermissionDenied(f"User {user} has no {permtype} permission on {doctype_name}.")
        try:
            doc = frappe.get_doc(doctype_name, document_name)
        except frappe.DoesNotExistError:
            raise PermissionDenied(TARGET_NOT_ACCESSIBLE)
        # Row level: User Permissions (company, branch...), if_owner, sharing.
        if not frappe.has_permission(doctype_name, "read", doc=doc, user=user) or not frappe.has_permission(
            doctype_name, permtype, doc=doc, user=user
        ):
            raise PermissionDenied(TARGET_NOT_ACCESSIBLE)
        if action_type == "Update":
            problems = blocked_fields(doctype_name, payload, user, mode="update")
            if problems:
                raise PermissionDenied(
                    f"These fields cannot be changed on {doctype_name} through the agent: "
                    f"{', '.join(problems)}"
                )
        return doc

    def _assert_create_allowed(self, doctype_name, payload, user):
        if not doctype_name or not frappe.db.exists("DocType", doctype_name):
            raise PermissionDenied(f"DocType '{doctype_name}' not found.")
        if not frappe.has_permission(doctype_name, "create", user=user):
            raise PermissionDenied(f"User {user} has no create permission on {doctype_name}.")
        if isinstance(payload, dict) and payload.get("doctype") not in (None, doctype_name):
            # The executed insert uses payload["doctype"]: it must be the checked DocType.
            raise PermissionDenied("Payload doctype does not match the requested DocType.")
        problems = blocked_fields(doctype_name, payload, user, mode="create")
        if problems:
            raise PermissionDenied(
                f"These fields cannot be set on a new {doctype_name} through the agent: "
                f"{', '.join(problems)}"
            )

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
