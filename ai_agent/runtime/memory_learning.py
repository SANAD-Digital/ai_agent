"""Learn structured mappings from human-approved actions.

Model-written episode summaries are unreliable; the ground truth for
"supplier X posts to account Y" is the document a human actually approved.
After an approval executes, this module mines its payload for entity→account
mappings and upserts them as Mapping memories (one fact per entity per
company, latest approval wins).
"""

import json
from collections import Counter

import frappe

from ai_agent.runtime.memory_service import MemoryService

LEARNABLE_DOCTYPES = {"Journal Entry", "Purchase Invoice"}


def learn_from_executed_approval(approval):
    """Best-effort mapping extraction from an executed approval.

    Creates carry the full document payload; Updates carry the corrected
    fields a human explicitly fixed — the strongest learning signal of all.
    """
    service = MemoryService()
    if not service.is_enabled():
        return 0
    if approval.action_type not in ("Create", "Bulk Create", "Update"):
        return 0

    run_agent = (
        frappe.db.get_value("AI Run", approval.run, "agent") if approval.run else None
    )
    if run_agent and frappe.db.get_value("AI Agent", run_agent, "disable_memory"):
        return 0

    payload = json.loads(approval.payload_json or "{}")
    if approval.action_type == "Bulk Create":
        documents = [
            document.get("payload")
            for document in payload.get("documents") or []
            if isinstance(document, dict)
        ]
    else:
        documents = [payload]

    learned = 0
    for document in documents:
        if not isinstance(document, dict):
            continue
        doc_type = document.get("doctype") or approval.doctype_name
        if doc_type not in LEARNABLE_DOCTYPES:
            continue
        if approval.action_type == "Update":
            document = _complete_update_document(approval, document)
        if doc_type == "Journal Entry":
            learned += _learn_journal_entry_mappings(service, approval, document, run_agent)
        elif doc_type == "Purchase Invoice":
            learned += _learn_purchase_invoice_mapping(service, approval, document, run_agent)
    return learned


def _complete_update_document(approval, document):
    """Update payloads are partial; pull the company from the saved document."""
    completed = dict(document)
    if not completed.get("company") and approval.document_name:
        completed["company"] = frappe.db.get_value(
            approval.doctype_name, approval.document_name, "company"
        )
    return completed


def _learn_journal_entry_mappings(service, approval, document, run_agent):
    company = document.get("company")
    if not company and not run_agent:
        return 0

    learned = 0
    for row in document.get("accounts") or []:
        if not isinstance(row, dict) or not row.get("account"):
            continue
        entity_type, entity_name = _row_entity(row)
        if not entity_name:
            continue
        mapping = {"account": row.get("account")}
        if row.get("cost_center"):
            mapping["cost_center"] = row.get("cost_center")
        memory = service.store(
            user=approval.requested_by,
            scope="Company" if company else "Agent",
            memory_type="Mapping",
            content=(
                f"{entity_type} '{entity_name}' → account '{row.get('account')}' "
                "(learned from an approved Journal Entry)."
            ),
            company=company,
            agent=run_agent,
            sensitivity="Medium",
            source_run=None,
            entity_type=entity_type,
            entity_key=entity_name,
            mapping=mapping,
        )
        if memory:
            learned += 1
    return learned


def _learn_purchase_invoice_mapping(service, approval, document, run_agent):
    """One supplier→expense-account fact per approved invoice (majority vote
    across item rows when they disagree)."""
    supplier = document.get("supplier")
    company = document.get("company")
    if not supplier or (not company and not run_agent):
        return 0

    accounts = [
        row.get("expense_account")
        for row in document.get("items") or []
        if isinstance(row, dict) and row.get("expense_account")
    ]
    if not accounts:
        return 0
    top_account = Counter(accounts).most_common(1)[0][0]

    memory = service.store(
        user=approval.requested_by,
        scope="Company" if company else "Agent",
        memory_type="Mapping",
        content=(
            f"Supplier '{supplier}' → expense account '{top_account}' "
            "(learned from an approved Purchase Invoice)."
        ),
        company=company,
        agent=run_agent,
        sensitivity="Medium",
        entity_type="Supplier",
        entity_key=supplier,
        mapping={"expense_account": top_account},
    )
    return 1 if memory else 0


def _row_entity(row):
    if row.get("custom_cash_supplier"):
        return "Cash Supplier", row.get("custom_cash_supplier")
    if row.get("party") and row.get("party_type"):
        return str(row.get("party_type")), row.get("party")
    return "", ""
