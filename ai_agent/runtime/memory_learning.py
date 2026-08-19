"""Learn structured mappings from human-approved actions.

Model-written episode summaries are unreliable; the ground truth for
"supplier X posts to account Y" is the document a human actually approved.
After an approval executes, this module mines its payload for entity→account
mappings and upserts them as Mapping memories (one fact per entity per
company, latest approval wins).
"""

import json

import frappe

from ai_agent.runtime.memory_service import MemoryService

LEARNABLE_DOCTYPES = {"Journal Entry"}


def learn_from_executed_approval(approval):
    """Best-effort mapping extraction from an executed Create/Bulk Create."""
    service = MemoryService()
    if not service.is_enabled():
        return 0
    if approval.action_type not in ("Create", "Bulk Create"):
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
        if (document.get("doctype") or approval.doctype_name) not in LEARNABLE_DOCTYPES:
            continue
        learned += _learn_journal_entry_mappings(service, approval, document, run_agent)
    return learned


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


def _row_entity(row):
    if row.get("custom_cash_supplier"):
        return "Cash Supplier", row.get("custom_cash_supplier")
    if row.get("party") and row.get("party_type"):
        return str(row.get("party_type")), row.get("party")
    return "", ""
