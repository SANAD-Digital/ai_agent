"""Route actions.request_* tool calls into approval requests.

Extracted from the orchestrator. The run does not pause here: it finalizes
with a pending-approval answer, the human decision executes later via
api.approve_action, and the conversation continues in a new run chained by
parent_run (which carries the approval outcome in its session context).
"""

import hashlib
import json

import frappe
from frappe import _

from ai_agent.runtime.exceptions import PermissionDenied, ToolDenied

def _customize_request_tools():
    from ai_agent.customization import registry

    return (*registry.REQUEST_TOOLS, registry.UNDO_TOOL)


KNOWN_ACTION_TOOLS = {
    "actions.request_create_draft",
    "actions.request_bulk_create",
    "actions.request_update",
    "actions.request_submit",
    "actions.request_cancel",
    "actions.request_workflow_action",
    "actions.request_mapped_draft",
    "messaging.request_email",
    # customization: each one becomes a "Customize" approval (ai_agent.customization.tools.route);
    # the list is the registry's, so a new kind needs no change here
    *_customize_request_tools(),
}


class ActionInterceptor:
    def __init__(self, *, run_name, agent, permission_context, approval_engine):
        self.run_name = run_name
        self.agent = agent
        self.ctx = permission_context
        self.approval_engine = approval_engine

    def handle_action_tool(self, tool_name, arguments):
        """Create the approval request that represents this action tool call."""
        if tool_name not in KNOWN_ACTION_TOOLS:
            return {"error": f"Unknown action tool: {tool_name}"}

        if tool_name.startswith("customize."):
            from ai_agent.customization import tools as customization_tools

            return customization_tools.route(tool_name, arguments, self.ctx, self.run_name)

        if tool_name == "actions.request_create_draft":
            payload = self._document_payload(arguments)
            approval = self._create_or_get_create_approval(arguments.get("doctype"), payload)
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_bulk_create":
            documents = self._bulk_create_documents(arguments.get("documents") or [])
            if not documents:
                return {"error": "documents is required"}
            denied = [
                document["doctype"]
                for document in documents
                if not self.ctx.has_doctype_permission(document["doctype"], "create")
            ]
            if denied:
                return {"error": f"No create permission on: {', '.join(sorted(set(denied)))}"}
            approval = self._create_or_get_bulk_create_approval(
                documents,
                arguments.get("summary"),
            )
            return {
                "approval_required": True,
                "approval_id": approval.name,
                "document_count": len(documents),
                "doctypes": sorted({document["doctype"] for document in documents}),
            }

        elif tool_name == "actions.request_mapped_draft":
            from ai_agent.tools.erpnext_native import build_mapped_payload

            target = arguments.get("target_doctype")
            try:
                payload = build_mapped_payload(
                    arguments.get("source_doctype"), arguments.get("source_name"), target, self.ctx
                )
            except PermissionDenied as exc:
                return {"error": str(exc)}
            approval = self._create_or_get_create_approval(target, payload)
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_update":
            payload = self._document_payload(arguments, include_doctype=False)
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Update",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=f"Update {arguments.get('doctype')} {arguments.get('document_name')}",
                payload=payload,
                expected_modified=arguments.get("expected_modified"),
            )
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "messaging.request_email":
            from ai_agent.tools import messaging

            try:
                payload = messaging.build_payload(arguments)
            except frappe.ValidationError as exc:
                return {"error": str(exc)}
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Approved Method",
                doctype_name=payload.get("reference_doctype"),
                document_name=payload.get("reference_name"),
                action_summary=messaging.summary(payload),
                impact_summary=_("The email will be sent from the system's outgoing email account and recorded in the communications."),
                payload=payload,
            )
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_submit":
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Submit",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=f"Submit {arguments.get('doctype')} {arguments.get('document_name')}",
                impact_summary="The document will be submitted.",
                payload={"doctype": arguments.get("doctype"), "name": arguments.get("document_name")},
            )
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_cancel":
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Cancel",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=f"Cancel {arguments.get('doctype')} {arguments.get('document_name')}",
                impact_summary="The document will be cancelled.",
                payload={"doctype": arguments.get("doctype"), "name": arguments.get("document_name")},
            )
            return {"approval_required": True, "approval_id": approval.name}

        elif tool_name == "actions.request_workflow_action":
            approval = self.approval_engine.create_approval_request(
                run_name=self.run_name,
                action_type="Workflow",
                doctype_name=arguments.get("doctype"),
                document_name=arguments.get("document_name"),
                action_summary=(
                    f"Workflow action '{arguments.get('action')}' on "
                    f"{arguments.get('doctype')} {arguments.get('document_name')}"
                ),
                impact_summary=self._workflow_impact(arguments),
                payload={
                    "doctype": arguments.get("doctype"),
                    "name": arguments.get("document_name"),
                    "action": arguments.get("action"),
                },
            )
            return {"approval_required": True, "approval_id": approval.name}

        return {"error": f"Unknown action tool: {tool_name}"}

    @staticmethod
    def _workflow_impact(arguments):
        """"State A → State B" for the approval card, when the transition is known."""
        try:
            from ai_agent.tools.workflow import workflow_of

            doctype, name, action = arguments.get("doctype"), arguments.get("document_name"), arguments.get("action")
            workflow = workflow_of(doctype)
            if workflow and name and frappe.db.exists(doctype, name):
                state = frappe.db.get_value(doctype, name, workflow.workflow_state_field or "workflow_state")
                nxt = next((t.next_state for t in workflow.transitions if t.state == state and t.action == action), None)
                if nxt:
                    return f"{state} → {nxt}"
        except Exception:
            # Cosmetic approval-card text: the generic sentence below is enough.
            pass
        return "The workflow action will be applied to the document."

    def maybe_create_approval_from_prepare(self, tool_name, result):
        if tool_name != "actions.prepare_document" or not isinstance(result, dict):
            return result
        if result.get("error") or result.get("missing_fields"):
            return result
        if self.agent.get("read_only") or not self.agent.get("allow_actions"):
            return result

        doc_type = result.get("doctype")
        payload = self._document_payload({
            "doctype": doc_type,
            "payload": result.get("payload") or {},
        })
        if not doc_type or not payload:
            return result

        try:
            approval = self._create_or_get_create_approval(doc_type, payload)
        except PermissionDenied as exc:
            enriched = dict(result)
            enriched["approval_error"] = str(exc)
            return enriched

        enriched = dict(result)
        enriched.update({
            "approval_required": True,
            "approval_id": approval.name,
            "approval_status": approval.status,
        })
        return enriched

    def _create_or_get_create_approval(self, doc_type, payload):
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.run_name}:create:{doc_type}:{payload_hash}".encode()
        ).hexdigest()

        existing = frappe.db.exists("AI Approval Request", {"idempotency_key": idempotency_key})
        if not existing:
            existing = frappe.db.exists("AI Approval Request", {
                "run": self.run_name,
                "action_type": "Create",
                "doctype_name": doc_type,
                "payload_hash": payload_hash,
                "status": ("in", ["Pending", "Approved"]),
            })
        if existing:
            return frappe.get_doc("AI Approval Request", existing)

        approval = self.approval_engine.create_approval_request(
            run_name=self.run_name,
            action_type="Create",
            doctype_name=doc_type,
            action_summary=f"Create {doc_type} draft",
            impact_summary=f"A new {doc_type} will be created after approval.",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return approval

    def _create_or_get_bulk_create_approval(self, documents, summary=None):
        payload = {"documents": documents}
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
        idempotency_key = hashlib.sha256(
            f"{self.run_name}:bulk-create:{payload_hash}".encode()
        ).hexdigest()

        existing = frappe.db.exists("AI Approval Request", {"idempotency_key": idempotency_key})
        if not existing:
            existing = frappe.db.exists("AI Approval Request", {
                "run": self.run_name,
                "action_type": "Bulk Create",
                "payload_hash": payload_hash,
                "status": ("in", ["Pending", "Approved"]),
            })
        if existing:
            return frappe.get_doc("AI Approval Request", existing)

        doctypes = sorted({document["doctype"] for document in documents})
        action_summary = summary or f"Create {len(documents)} draft documents"
        approval = self.approval_engine.create_approval_request(
            run_name=self.run_name,
            action_type="Bulk Create",
            doctype_name=doctypes[0] if len(doctypes) == 1 else None,
            action_summary=action_summary,
            impact_summary=f"{len(documents)} draft documents will be created after one approval.",
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return approval

    def _document_payload(self, arguments, include_doctype=True):
        payload = dict(arguments.get("payload") or {})
        if include_doctype and arguments.get("doctype"):
            payload["doctype"] = arguments.get("doctype")
        return payload

    def _bulk_create_documents(self, documents):
        if len(documents) > 100:
            raise ToolDenied("Bulk create supports up to 100 documents per approval.")
        normalized = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            payload = dict(document.get("payload") or {})
            doc_type = document.get("doctype") or payload.get("doctype")
            if not doc_type or not payload:
                continue
            payload["doctype"] = doc_type
            normalized.append({"doctype": doc_type, "payload": payload})
        return normalized
