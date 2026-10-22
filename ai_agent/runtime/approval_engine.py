import json
import hashlib
import frappe
from ai_agent.runtime.constants import REALTIME_EVENTS
from ai_agent.runtime.exceptions import (
    ApprovalExpired, ApprovalAlreadyExecuted,
    PermissionDenied, IdempotencyConflict,
)
# Request checks and per-type execution live in their own modules; ApprovalEngine combines them.
from ai_agent.runtime.approval_checks import CUSTOMIZE, ApprovalChecks
from ai_agent.runtime.approval_execution import ApprovalExecution, failure_text


class DecisionOutcome:
    """What a decision produced: the reloaded request, the execution result or the error text, and whether it
    only reported an earlier decision."""

    def __init__(self, approval, result, error, repeated=False):
        self.approval, self.result, self.error, self.repeated = approval, result, error, repeated


class ApprovalEngine(ApprovalChecks, ApprovalExecution):
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
        # The document the person approves is the one executed: Submit/Cancel/Workflow act on its state too.
        if action_type in ("Update", "Submit", "Cancel", "Workflow") and not expected_modified and target is not None:
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
        if action_type == CUSTOMIZE:
            from ai_agent.customization import policy as customization_policy

            ttl = customization_policy.approval_ttl_minutes()

        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()

        now = frappe.utils.now_datetime()
        expires_at = frappe.utils.add_to_date(now, minutes=ttl)

        if getattr(self.ctx, "surface", None) == "MCP" and not idempotency_key:
            # External hosts retry a tool call they think timed out: the same request from the same user
            # within two minutes is the pending one, not a second card (or, in direct mode, a second document).
            duplicate = frappe.db.get_value("AI Approval Request", {
                "requested_by": frappe.session.user, "action_type": action_type, "doctype_name": doctype_name,
                "document_name": document_name or ("is", "not set"), "payload_hash": payload_hash, "status": "Pending",
                "creation": (">=", frappe.utils.add_to_date(now, minutes=-2)),
            }, "name")
            if duplicate:
                return frappe.get_doc("AI Approval Request", duplicate)

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

    def decide(self, approval_name, decision, user=None, reason=None, on_event=None):
        """Approve-and-execute, or reject: the one decision path behind the Desk chat card, the MCP card and the
        ERPNext form. Exactly once: a repeated or concurrent decision returns the recorded outcome
        (``repeated=True``) and never executes twice. ``on_event(name, **data)`` lets a caller log the stages."""
        user = user or frappe.session.user
        note = on_event or (lambda *args, **kwargs: None)
        if decision not in ("approve", "reject"):
            frappe.throw(frappe._("decision must be approve or reject"))
        approval = frappe.get_doc("AI Approval Request", approval_name)
        if decision == "reject":
            if approval.status == "Rejected":
                return DecisionOutcome(approval, None, None, repeated=True)
            approval = self.reject(approval_name, reason=reason, user=user)
            note("rejected")
            return DecisionOutcome(approval, None, None)
        if approval.status == "Executed":
            return DecisionOutcome(approval, None, None, repeated=True)
        if approval.status == "Pending":
            try:
                self.approve(approval_name, user=user)
            except ApprovalAlreadyExecuted:
                approval.reload()   # a concurrent decision got there first
                return DecisionOutcome(approval, None, None, repeated=True)
            note("approved")
        note("execute_started")
        try:
            result = self.execute_approved_action(approval_name)
        except ApprovalAlreadyExecuted:
            approval.reload()
            return DecisionOutcome(approval, None, None, repeated=True)
        except ApprovalExpired:
            raise
        except Exception as exc:
            error = failure_text(exc)
            note("failed", error=error[:200])
            approval.reload()
            return DecisionOutcome(approval, None, error)
        note("execute_completed")
        approval.reload()
        return DecisionOutcome(approval, result, None)

    def approve(self, approval_name, user=None):
        """Approve an approval request."""
        user = user or frappe.session.user
        if user == "Guest":
            raise PermissionDenied("Guest users cannot approve actions.")

        # SELECT ... FOR UPDATE: a concurrent second decision waits for the first and then sees its final status.
        approval = frappe.get_doc("AI Approval Request", approval_name, for_update=True)

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

        if approval.action_type == CUSTOMIZE:
            # Its own four-eyes switch, and the approver must hold the kind's roles: they execute it.
            from ai_agent.customization import policy as customization_policy

            customization_policy.assert_can_approve(approval, user)

        approval.status = "Approved"
        approval.approved_by = user
        approval.decided_at = frappe.utils.now_datetime()
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return approval

    def reject(self, approval_name, reason=None, user=None):
        """Reject an approval request."""
        user = user or frappe.session.user
        approval = frappe.get_doc("AI Approval Request", approval_name, for_update=True)

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
        """Execute an approved action, exactly once."""
        approval = frappe.get_doc("AI Approval Request", approval_name, for_update=True)

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
        # The payload executed is the one approved, for every action type (the hash was checked only at approve).
        if approval.payload_hash != self._compute_hash(approval.payload_json or ""):
            # Ends Failed (never left Approved): a changed payload is never retried.
            exc = PermissionDenied("Payload has been tampered with.")
            self._mark_failed(approval_name, exc)
            raise exc
        self.assert_action_allowed(
            approval.action_type,
            approval.doctype_name,
            approval.document_name,
            json.loads(approval.payload_json or "{}"),
            user,
            phase="execute",
        )

        try:
            result = self._execute_by_type(approval)
        except Exception as exc:
            frappe.db.rollback()
            self._mark_failed(approval_name, exc)
            raise
        try:
            from ai_agent.runtime.memory_learning import learn_from_executed_approval

            learn_from_executed_approval(approval)
        except Exception:
            # Learning is best-effort: it must never fail an executed action.
            frappe.log_error(
                frappe.get_traceback(), f"AI Memory Learning Failed: {approval.name}"
            )
        return result

    def _mark_failed(self, approval_name, exc):
        """A failed execution ends as Failed (never left Approved, which would invite a blind retry)."""
        frappe.log_error(frappe.get_traceback(), f"AI Approval execution failed: {approval_name}")
        approval = frappe.get_doc("AI Approval Request", approval_name, for_update=True)
        if approval.status in ("Executed", "Failed"):
            return
        approval.status = "Failed"
        approval.executed_at = frappe.utils.now_datetime()
        result = {"error": failure_text(exc)[:500]}
        created = getattr(exc, "created_before_compensation", None)
        if created is not None:
            # A multi-record change failed part-way: what was created and then removed again.
            result.update({"created_before_compensation": list(created), "compensated": True})
        approval.execution_result = json.dumps(result, ensure_ascii=False, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

    def execute_direct(self, approval_name, user):
        """Approval is not required by policy: the requester's own request runs at once, under the requester's permissions.

        Same validation and the same executor as an approved request; only the human step is skipped.
        """
        from ai_agent.runtime import policy

        approval = frappe.get_doc("AI Approval Request", approval_name, for_update=True)
        if policy.requires_human(approval.action_type):
            raise PermissionDenied(f"A {approval.action_type} request is never run without a person approving it.")
        if approval.status != "Pending":
            raise ApprovalAlreadyExecuted(f"Request is already {approval.status}.")
        if approval.requested_by != user:
            raise PermissionDenied("Only the requester can run this request directly.")
        if approval.payload_hash != self._compute_hash(approval.payload_json):
            raise PermissionDenied("Payload has been tampered with.")
        approval.status = "Approved"
        approval.approved_by = user
        approval.decided_at = frappe.utils.now_datetime()
        approval.save(ignore_permissions=True)
        frappe.db.commit()
        return self.execute_approved_action(approval.name)

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

    def expire_pending(self):
        """Expire all pending approvals past their TTL."""
        now = frappe.utils.now_datetime()
        pending = frappe.get_all(
            "AI Approval Request",
            filters={"status": "Pending", "expires_at": ("<", now)},
            pluck="name",
        )
        from ai_agent.ai_agent.doctype.ai_approval_request.ai_approval_request import settle_tool_call

        for name in pending:
            frappe.db.set_value("AI Approval Request", name, "status", "Expired", update_modified=False)
            run, created = frappe.db.get_value("AI Approval Request", name, ["run", "creation"])
            settle_tool_call(name, run, "Expired", created)
            try:
                from ai_agent.engine.approvals import on_approval_expired

                on_approval_expired(name)
            except Exception:
                frappe.log_error(frappe.get_traceback(), f"AI approval expiry: chat update failed for {name}")
        if pending:
            frappe.db.commit()
        return len(pending)


def expire_pending_approvals():
    """Scheduled task to expire pending approvals."""
    from ai_agent.runtime.permission_context import PermissionContext
    engine = ApprovalEngine(PermissionContext("Administrator"))
    return engine.expire_pending()
