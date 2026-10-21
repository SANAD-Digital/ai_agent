# Workflows (customize.request_workflow) — Frappe v16

Verified against `frappe/workflow/doctype/workflow/workflow.py` (validate, set_active, on_update),
`frappe/model/workflow.py` (get_transitions, apply_workflow, validate_workflow, set_workflow_state_on_action) and
`workflow.js` (Frappe 16.28). The example passes the checker on a live site.

## 1. When to use it

The user wants documents to go through people: "invoices above 10,000 need the manager's approval", "a request is
reviewed, then approved or rejected", "only HR may approve leave". A workflow adds a **state** to every document,
**action buttons** (Approve, Reject…) for the roles you name, and, on a submittable DocType, it **replaces the
Submit and Cancel buttons**: documents are submitted and cancelled only through the actions.

Not for: sending an email (Notification), setting a field on save (Server Script), hiding a field (properties).

Read first: `customize__inspect` with `include: ["workflows"]` lists the DocType's workflows and which one is active;
`schema__describe_workflow` shows the active one's states and transitions.

## 2. Arguments

`operation`: `create` | `update` | `activate` | `deactivate`; `workflow_name`; `purpose`.

- `create`: `doctype`, `states`, `transitions`, optional `activate` (default 1), `send_email_alert`, `override_status`,
  `sample_document`.
- `update`: `states` and/or `transitions` (each **replaces the whole list**: send all rows, changed or not),
  `send_email_alert`, `override_status`. The DocType never changes; activation changes only with activate/deactivate.
- `activate` / `deactivate`: only `workflow_name`.

**states** (order matters: the **first** state is where every new document starts, so it is a draft state):

| Key | Meaning |
|---|---|
| `state` | Name, e.g. `Pending Approval`. A missing Workflow State record is created (`style`: Primary, Info, Success, Warning, Danger, Inverse) |
| `doc_status` | `0` draft, `1` submitted, `2` cancelled. 1 and 2 only on submittable DocTypes |
| `allow_edit` | The role that may edit the document while it is in this state |
| `update_field` / `update_value` | Optional: set this field when the document enters the state (`evaluate_as_expression: 1` = the value is a Python expression on `doc`) |
| `is_optional_state` | 1 = no workflow-action emails for this state |
| `message` | Text shown on the form |

**transitions**: `state` → `action` (button label; a missing Workflow Action record is created) → `next_state`,
`allowed` (the role that sees the button), `allow_self_approval` (default 1; 0 = the creator cannot press it),
`condition` (optional Python on `doc`, e.g. `doc.grand_total > 10000`; only `frappe.db.get_value`,
`frappe.db.get_list`, `frappe.session` and `frappe.utils.now_datetime/add_to_date/get_datetime/now` are available).

```json customize kind=workflow
{"operation": "create", "workflow_name": "Journal Entry Approval", "doctype": "Journal Entry",
 "purpose": "Journal entries are reviewed by an accounts manager before they are posted",
 "states": [
   {"state": "Draft", "doc_status": 0, "allow_edit": "Accounts User"},
   {"state": "Pending Approval", "doc_status": 0, "allow_edit": "Accounts Manager", "style": "Warning"},
   {"state": "Approved", "doc_status": 1, "allow_edit": "Accounts Manager"},
   {"state": "Rejected", "doc_status": 0, "allow_edit": "Accounts User"},
   {"state": "Cancelled", "doc_status": 2, "allow_edit": "Accounts Manager"}],
 "transitions": [
   {"state": "Draft", "action": "Review", "next_state": "Pending Approval", "allowed": "Accounts User"},
   {"state": "Pending Approval", "action": "Approve", "next_state": "Approved", "allowed": "Accounts Manager", "allow_self_approval": 0},
   {"state": "Pending Approval", "action": "Reject", "next_state": "Rejected", "allowed": "Accounts Manager"},
   {"state": "Rejected", "action": "Review", "next_state": "Pending Approval", "allowed": "Accounts User"},
   {"state": "Approved", "action": "Cancel", "next_state": "Cancelled", "allowed": "Accounts Manager"}]}
```

## 3. What the checker refuses (critical) and how to fix it

| Code | Rule (Frappe source) | Fix |
|---|---|---|
| `workflow.first_state` | New documents start in `states[0]` (`validate_workflow`) | Put a draft (doc_status 0) state first |
| `workflow.doc_status` | 1/2 need a submittable DocType | Use doc_status 0 everywhere, or another DocType |
| `workflow.transition_state` | Every transition names listed states (`validate_docstatus`: "not a valid State") | Add the state or fix the spelling |
| `workflow.docstatus` | Never from cancelled (2); never submitted (1) → draft (0); never draft (0) → cancelled (2) | Draft → submitted → cancelled only |
| `workflow.unreachable` | A state no transition reaches from the first one (cancelled states count as reached once a submitted state is, through the form's Cancel) | Add a transition into it or drop it |
| `workflow.state_duplicate` | A state listed twice | One row per state |
| `workflow.role` | The role does not exist, or is Guest | Use an existing role (`allow_edit`, `allowed`) |
| `workflow.condition` | The condition is not Python, or fails on a real document (evaluated with `frappe.safe_eval` on `sample_document` or the latest document) | Fix the expression; use `doc.field` names that exist |
| `workflow.update_field` / `workflow.update_value` | The field must exist and hold a value; not `name`, `docstatus`, `workflow_state`; the expression must evaluate | Pick another field |
| `workflow.transition_tasks` | The existing workflow has transition tasks (code) | A person changes it by hand |
| `workflow.submit_cancel_policy` | AI Settings "Allow Submit/Cancel Actions" is off: transitions that change doc_status are refused (design §4.2) | Ask the admin, or keep every transition within doc_status 0 |
| `workflow.doctype` | Core, single, child-table or protected DocTypes | — |
| `roles.missing` | The requester (and the approver) need System Manager | — |

Warnings (allowed, shown on the card): a role no enabled user holds (`workflow.role_unheld`), a role without
write/submit/cancel permission for its transition (`workflow.role_permission`), a draft state with no way out
(`workflow.dead_end`, fine for a final "Rejected"), a submittable DocType no transition submits
(`workflow.never_submits`), the same action from the same state for the same role twice (`workflow.transition_duplicate`).

## 4. Impact you must tell the user

- **Only one workflow is active per DocType**: activating one deactivates the current one (the card names it).
- Frappe adds a hidden `workflow_state` field to the DocType the first time (it stays after an undo).
- On activation every existing document **with an empty state** gets the first state of its doc_status; documents
  whose doc_status has no state show no state (the card counts both).
- On a submittable DocType, users submit and cancel only through the workflow.

## 5. Undo

`customize.request_undo` on the executed approval: a create is **deleted** (with the Workflow State / Workflow Action
records it created, unless something still uses them) and the previously active workflow is reactivated; an update
restores the old states/transitions; activate/deactivate are reversed. The `workflow_state` values written into
documents stay.
