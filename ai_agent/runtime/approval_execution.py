"""Execution of an approved request, one method per action type. Each write and the request's bookkeeping share
one commit, so a crash cannot leave an executed document behind a still-Approved request. A mixin of
``ApprovalEngine``."""

import json

import frappe

from ai_agent.runtime.approval_checks import CUSTOMIZE, _approved_methods
from ai_agent.runtime.exceptions import ApprovalRequired, ConcurrencyConflict, PermissionDenied
from ai_agent.runtime.write_guard import foreign_child_rows


def failure_text(exc):
    """Never an empty reason: some Frappe errors carry their text only in message_log (msgprint before raise)."""
    text = frappe.utils.strip_html(str(exc) or "").strip()
    if not text:
        for entry in reversed(getattr(frappe.local, "message_log", None) or []):
            try:
                message = json.loads(entry).get("message") if isinstance(entry, str) else (entry or {}).get("message")
            except ValueError:
                message = entry
            if message:
                text = frappe.utils.strip_html(str(message)).strip()
                break
    return text or type(exc).__name__


class ApprovalExecution:
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
        elif approval.action_type == "Approved Method":
            _check, execute = _approved_methods()[payload.get("method")]
            return execute(payload, approval)
        elif approval.action_type == CUSTOMIZE:
            from ai_agent.customization import executor

            return executor.run(approval, approval.approved_by)
        else:
            raise ApprovalRequired(f"Action type '{approval.action_type}' not yet implemented.")

    def _execute_create(self, approval, payload):
        """Execute a create draft action.

        The document write and the approval bookkeeping share one commit so a
        crash cannot leave an executed document behind a still-Approved
        request (which would invite a duplicate re-execution).
        """
        # The approved DocType is authoritative: request tools send the payload without a "doctype" key, and
        # assert_action_allowed has already refused a payload that names a different one.
        doc = frappe.get_doc({**payload, "doctype": approval.doctype_name})
        doc.insert(ignore_permissions=False)

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name, "doctype": approval.doctype_name}, default=str)
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
        except Exception as exc:
            # The traceback goes to the Error Log only; the user and the model get the reason.
            frappe.log_error(frappe.get_traceback(), f"AI Approval bulk create failed: {approval.name}")
            frappe.db.rollback()
            approval.status = "Failed"
            approval.executed_at = frappe.utils.now_datetime()
            approval.execution_result = json.dumps(
                {
                    "error": failure_text(exc)[:500],
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
        foreign = foreign_child_rows(doc, payload)
        if foreign:
            raise PermissionDenied(f"These rows do not belong to {doc.doctype} {doc.name}: {', '.join(foreign)}.")

        for key, val in payload.items():
            doc.set(key, val)

        doc.save(ignore_permissions=False)

        approval.status = "Executed"
        approval.executed_at = frappe.utils.now_datetime()
        approval.execution_result = json.dumps({"document_name": doc.name}, default=str)
        approval.save(ignore_permissions=True)
        frappe.db.commit()

        return {"document_name": doc.name, "doctype": approval.doctype_name}

    @staticmethod
    def _assert_unchanged(approval, doc):
        if approval.expected_modified and str(doc.modified) != str(approval.expected_modified):
            raise ConcurrencyConflict("The document was changed after the approval was requested; ask for a new request.")

    def _execute_submit(self, approval):
        """Execute a submit action."""
        doc = frappe.get_doc(approval.doctype_name, approval.document_name)
        self._assert_unchanged(approval, doc)
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
        self._assert_unchanged(approval, doc)
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
        self._assert_unchanged(approval, doc)
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
