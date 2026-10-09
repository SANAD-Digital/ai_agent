---
name: document-workflow
description: Use for submitting, cancelling, and performing workflow actions on existing documents.
tools: schema.search_doctypes, schema.describe_doctype, schema.describe_workflow, data.get_document, actions.request_update, actions.request_submit, actions.request_cancel, actions.request_workflow_action
---

# Document Workflow

1. First check the document's current status and workflow state.
2. Verify the user has the required permissions for the action.
3. Present the action impact clearly before requesting approval.
4. Submit and Cancel require separate approval from Create/Update.
5. Use the correct workflow API for state transitions.
6. Reload the document after workflow execution to confirm state.
7. Report any validation errors or workflow restrictions.
