---
name: create_purchase_invoice_from_analysis
description: Prepare draft Purchase Invoice creation from reviewed analysis after explicit user action.
tools: schema.describe_doctype, data.query, data.get_document, actions.prepare_document, actions.validate_document, actions.request_create_draft
---

# Create Purchase Invoice From Analysis

Use only after the user explicitly requests draft creation in the workspace.

1. Create only draft Purchase Invoices; never submit.
2. Use reviewed data, not raw model guesses.
3. Validate duplicate bill number and required fields before creation.
4. Attach original files to the created draft.
5. Add a comment indicating the draft came from Purchase Invoice AI Workspace.
