---
name: diagnose-gl-entries
description: Why an accounting entry (GL) does not show for a document.
tools: diagnose.document, data.get_document, setup.check, knowledge.search
---

# Diagnose: accounting entry not showing

1. Call diagnose.document on the document.
2. Causes, in order: docstatus not submitted; gl_entry missing; missing company default accounts (setup.check area "accounts" gives the full list); frozen accounts date; closed accounting period; period closing voucher covering the date.
3. Explain the cause and the steps to fix it. Do not propose editing settings through the agent: settings are changed by an administrator in the desk.
