import frappe


LEGACY_AUTO_GRANTS = {
    "accounting-memory-to-account": {"Purchase Invoice AI"},
    "financial-attachments-to-journal-entry": {"Purchase Invoice AI"},
    "validate-journal-entry-proposal": {"Purchase Invoice AI"},
    "whatsapp-business-support": {"WhatsApp Technical Support"},
}


def execute():
    """Remove grants created by the old global implicit-skill behavior."""
    for skill, allowed_agents in LEGACY_AUTO_GRANTS.items():
        names = frappe.get_all(
            "AI Skill Grant",
            filters={
                "skill": skill,
                "grant_type": "All Users",
                "company": ["is", "not set"],
                "agent": ["not in", sorted(allowed_agents)],
            },
            pluck="name",
        )
        for name in names:
            frappe.delete_doc("AI Skill Grant", name, ignore_permissions=True)

    _remove_duplicate_grants()

    from ai_agent.purchase_invoice_ai.api import _ensure_purchase_invoice_agent
    from ai_agent.whatsapp_support.setup import ensure_support_agent, ensure_support_skill_grants

    _ensure_purchase_invoice_agent()
    support_agent = ensure_support_agent()
    ensure_support_skill_grants(support_agent)
    frappe.db.commit()


def _remove_duplicate_grants():
    rows = frappe.get_all(
        "AI Skill Grant",
        fields=["name", "agent", "skill", "grant_type", "role", "user", "company", "enabled", "creation"],
        order_by="creation asc",
        page_length=0,
    )
    seen = set()
    for row in rows:
        key = (
            row.agent,
            row.skill,
            row.grant_type,
            row.role or "",
            row.user or "",
            row.company or "",
            int(row.enabled or 0),
        )
        if key in seen:
            frappe.delete_doc("AI Skill Grant", row.name, ignore_permissions=True)
            continue
        seen.add(key)
