---
name: document-creation
description: Use for creating new documents, preparing drafts, and validating document payloads.
---

# Document Creation

1. Always use schema discovery first to understand required fields.
2. Prepare a complete payload with all mandatory fields.
3. Never save directly - always request user approval first.
4. Validate the payload using actions.validate_document.
5. Ask the user for missing required information.
6. Present the impact clearly before requesting approval.
7. After approval, execute the create action.
8. Return the document link to the user.
