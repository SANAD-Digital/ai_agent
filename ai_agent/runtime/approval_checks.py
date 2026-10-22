"""Who may request or execute an approval: the AI Settings switches, the write policy, Frappe permissions down to
the row, blocked fields and foreign child rows, workflow transitions, approved methods and customization kinds.
Checked for the requester and again for the approver at execution. A mixin of ``ApprovalEngine``."""

import frappe

from ai_agent.runtime.exceptions import ApprovalRequired, PermissionDenied
from ai_agent.runtime.write_guard import blocked_fields, foreign_child_rows, replacement_hints

PERMTYPE_BY_ACTION = {
    "Create": "create",
    "Update": "write",
    "Submit": "submit",
    "Cancel": "cancel",
    "Workflow": "write",
}
TARGET_NOT_ACCESSIBLE = "Target document not found or not accessible."
CUSTOMIZE = "Customize"


def _approved_methods():
    """The only methods an "Approved Method" approval may run: name -> (check, execute)."""
    from ai_agent.tools import messaging

    return {messaging.METHOD: (messaging.assert_email_allowed, messaging.send_email)}


class ApprovalChecks:
    def assert_action_allowed(self, action_type, doctype_name, document_name, payload, user, phase="request"):
        """Raise PermissionDenied unless ``user`` may perform this action.

        Returns the loaded target document for document-level actions. ``phase`` is "request" for the
        requester and "execute" for the approver at execution time.
        """
        from ai_agent.runtime import policy

        if action_type == CUSTOMIZE:
            self._assert_customize_allowed(payload, user, phase)
            return None

        if action_type in ("Submit", "Cancel") and not policy.submit_cancel_allowed():
            raise PermissionDenied(
                "The agent does not submit or cancel documents (disabled in AI Settings). "
                "The user can do it from the document."
            )
        if action_type == "Workflow" and not policy.submit_cancel_allowed():
            action = (payload or {}).get("action") if isinstance(payload, dict) else None
            if policy.workflow_changes_docstatus(doctype_name, document_name, action):
                raise PermissionDenied(
                    f"Workflow action '{action}' submits or cancels the document, which the agent may not do."
                )
        try:
            if action_type == "Bulk Create":
                for document in self._bulk_documents(payload):
                    policy.assert_write_target_allowed(document["doctype"], document["payload"])
            elif action_type in ("Create", "Update"):
                policy.assert_write_target_allowed(doctype_name, payload)
        except policy.PolicyViolation as exc:
            raise PermissionDenied(str(exc))

        if action_type == "Approved Method":
            method = (payload or {}).get("method") if isinstance(payload, dict) else None
            if method not in _approved_methods():
                raise PermissionDenied(f"Method '{method}' is not an approved method.")
            try:
                _approved_methods()[method][0](payload, user)
            except (frappe.PermissionError, frappe.ValidationError) as exc:
                raise PermissionDenied(str(exc))
            return None
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
        if action_type == "Workflow":
            self._assert_workflow_action(doctype_name, document_name, payload, user)
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
                hints = replacement_hints(doctype_name, problems, user)
                raise PermissionDenied(
                    f"These fields cannot be changed on {doctype_name} through the agent: "
                    f"{', '.join(problems)}" + (f". {'; '.join(hints)}." if hints else "")
                )
            foreign = foreign_child_rows(doc, payload)
            if foreign:
                raise PermissionDenied(
                    f"These rows do not belong to {doctype_name} {document_name}: {', '.join(foreign)}. "
                    "Leave out 'name' to add a new row, or use the name of one of its own rows."
                )
        return doc

    def _assert_customize_allowed(self, payload, user, phase):
        """Settings and Frappe roles of the kind, for the requester and again for the approver. The
        surface, rate and pending limits were checked by customization.tools.request; the snapshot
        comparison and the full re-check run in customization.executor."""
        from ai_agent.customization import policy as customization_policy
        from ai_agent.customization import registry
        from ai_agent.customization.contract import CustomizationError

        if not isinstance(payload, dict) or not payload.get("kind") or not isinstance(payload.get("spec"), dict):
            raise PermissionDenied("A Customize request needs a kind and a spec.")
        try:
            kind = registry.get(payload["kind"])
        except CustomizationError as exc:
            raise PermissionDenied(str(exc))
        # "request" here is the engine's own re-check of the requester (no surface: tools.request did that).
        customization_policy.assert_allowed(
            payload["kind"], payload["spec"], user, "execute" if phase == "execute" else "requester", kind=kind
        )

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

    def _assert_workflow_action(self, doctype_name, document_name, payload, user):
        """The action must be one Frappe would offer ``user`` on the document now (role,
        condition, self-approval), checked at request time and again at execution."""
        from ai_agent.tools.workflow import available_transitions, workflow_of

        action = (payload or {}).get("action") if isinstance(payload, dict) else None
        workflow = workflow_of(doctype_name)
        if not workflow:
            raise PermissionDenied(f"{doctype_name} has no active workflow.")
        if not frappe.db.exists(doctype_name, document_name):
            raise PermissionDenied(TARGET_NOT_ACCESSIBLE)
        doc = frappe.get_doc(doctype_name, document_name)
        mine = available_transitions(doc, user, workflow)
        if action not in {t.action for t in mine}:
            state = doc.get(workflow.workflow_state_field or "workflow_state")
            offered = ", ".join(sorted({t.action for t in mine})) or "none"
            raise PermissionDenied(
                f"Workflow action '{action}' is not available to {user} on {doctype_name} {document_name} "
                f"in state '{state}'. Actions this user may take: {offered}."
            )
