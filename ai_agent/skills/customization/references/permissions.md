# Role permissions (customize.request_permissions) — Frappe v16

Verified against `frappe/core/page/permission_manager/permission_manager.py`, `frappe/permissions.py`
(`add_permission`, `update_permission_property`, `setup_custom_perms`, `reset_perms`, `get_role_permissions`),
`core/doctype/doctype/doctype.py` (`validate_permissions`) and `model/meta.py` (`set_custom_permissions`).

Use it when the user wants a role to see, edit, create, delete, submit… a DocType, or wants a right taken away, or
"put the permissions back to standard". Not for giving a role to a user (that is User / Role, blocked), not for
row-level filters (User Permission, blocked), not for one field's visibility alone (set the field's `permlevel`
with `customize.request_properties`, then grant `read`/`write` at that level here).

**Every request needs a second System Manager to approve it**: the requester can never approve their own
permission change, whatever the site's four-eyes setting says. Tell the user this when you request it.

## 1. How Frappe stores it

- Standard rules live in `DocPerm` (from the app's JSON). The Role Permission Manager never edits them: the first
  change copies them all into `Custom DocPerm`, and from then on only the Custom DocPerm rows count.
- `reset` deletes every Custom DocPerm of the DocType, and the standard rules apply again.
- One rule = role + `permlevel` + `if_owner`, with the rights `select read write create delete submit cancel amend
  print email report import export share`.
- Level 0 decides what a user can do with the document; level N (1–9) only decides read/write of fields whose
  `permlevel` is N. Frappe needs a level 0 rule for the role before a higher level, and drops create / submit /
  cancel / amend from levels above 0.
- `if_owner: 1` ("Only If Creator"): the rights apply only to documents the user created; Frappe refuses `report`
  on such a rule.
- Dependencies Frappe enforces: cancel needs submit; submit, cancel, amend need write; import needs create and
  an importable DocType; submit/amend need a submittable DocType; a rule needs at least one of select, read,
  write, create, submit, cancel.

## 2. Arguments

`operation` (`grant` | `revoke` | `reset`), `doctype`, and for grant/revoke: `role`, `rights` (list), `permlevel`
(default 0), `if_owner` (default 0). `purpose`: one line for the card. `allow_automatic_role: 1` only when the user
explicitly named Guest, All or Desk User. Unknown keys are refused.

- `grant`: sets the listed rights on the rule; the rule is added when the role has none at that level.
- `revoke` with `rights`: clears them; if no basic right is left, the whole rule goes. `revoke` without `rights`:
  removes the role's rule at that level.
- `reset`: only `doctype`.

Worked example — "let Sales User edit Site Visit, but only the ones they created, and never delete":

```json
{"operation": "grant", "doctype": "Site Visit", "role": "Sales User",
 "rights": ["read", "write", "create", "print"], "if_owner": 1,
 "purpose": "Sales users manage their own site visits"}
```

The card shows the rules before/after and every enabled user who gains or loses a right
(`ahmed@x.com gains read (own), write (own), create`). Run `customize.check` first with the same arguments.

## 3. Checks you will hit

| code | level | fix |
|---|---|---|
| `perm.doctype_protected` | critical | User, Role, Has Role, DocPerm, Custom DocPerm, DocType, Server/Client Script, AI *, any *Settings, Company, Fiscal Year… are never changed. Say so; do not look for a way around it |
| `perm.role_automatic` | critical | Guest / All = every user and website visitor. Name a real role |
| `perm.role_desk_user` | critical (write, create, delete, submit, cancel, amend, import) / warning (read-only) | Desk User = every desk user. Name a specific role |
| `perm.no_rule` | critical | revoke: the role has no rule at that level / if_owner. Read the rules (`customize.inspect`, part `permissions`) |
| `perm.no_change` | critical | the rules would be the same (already granted, or already standard) |
| `perm.empty` | critical | the DocType would have no rule at all |
| `perm.last_read` / `perm.last_write` | critical | nobody but Administrator could read/write it any more; keep another role |
| `perm.report_if_owner` | critical | drop `report`, or `if_owner` |
| `perm.frappe` | critical | Frappe's own rule (dependencies above, level 0 first, submittable/importable). Add the missing right (e.g. `write` with `submit`) or the level 0 rule first |
| `perm.frappe_adjusted` | warning | Frappe silently drops create/submit/cancel/amend above level 0 |
| `perm.risky_right` | warning | delete, cancel, submit: confirm the user means it |
| `perm.permlevel_unused` | warning | no field uses that level yet; set the field's permlevel first |
| `perm.role_disabled` | warning | the role is disabled |
| `perm.role_unused` | info | nobody holds the role yet: no user changes today |
| `roles.missing` | critical | the requester / approver needs System Manager |

## 4. Undo

`customize.request_undo` restores the exact Custom DocPerm rows that existed before (none = the standard rules).
It is a permission change too, so it also needs a second approver.
