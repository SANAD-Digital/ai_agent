import frappe
from frappe import _


class PermissionContext:
    """Builds and holds the effective permission context for an AI Run.

    Effective capability = Frappe User Permissions ∩ Agent Policy
    ∩ Skill Grant ∩ Skill Tool Policy ∩ Tool Gateway Policy.
    """

    def __init__(self, user, agent_name=None, company=None):
        self.user = user or frappe.session.user
        self.agent_name = agent_name
        self.company = company
        self._roles = None
        self._user_permissions = None
        self._child_parent_cache = {}

    @property
    def roles(self):
        if self._roles is None:
            self._roles = set(
                frappe.get_all(
                    "Has Role",
                    filters={"parent": self.user},
                    pluck="role",
                )
            )
        return self._roles

    @property
    def is_system_manager(self):
        return "System Manager" in self.roles

    @property
    def user_permissions(self):
        if self._user_permissions is None:
            self._user_permissions = frappe.permissions.get_user_permissions(self.user)
        return self._user_permissions

    def has_doctype_permission(self, doc_type, perm_type="read"):
        """Check if user has permission on a DocType."""
        if perm_type == "read" and self._is_child_table(doc_type):
            return self.has_child_table_read_permission(doc_type)
        return frappe.permissions.has_permission(
            doc_type,
            ptype=perm_type,
            user=self.user,
        )

    def has_child_table_read_permission(self, child_doctype):
        """Child tables inherit read access from any readable parent table field."""
        return bool(self.get_readable_parent_doctypes(child_doctype))

    def get_readable_parent_doctypes(self, child_doctype):
        """Return parent DocTypes whose table field grants read access to a child table."""
        return self._readable_parent_doctypes(child_doctype, seen=set())

    def _readable_parent_doctypes(self, child_doctype, seen):
        if child_doctype in seen:
            return []
        seen.add(child_doctype)

        cache_key = (child_doctype, tuple(sorted(seen)))
        if cache_key in self._child_parent_cache:
            return self._child_parent_cache[cache_key]

        parent_fields = frappe.get_all(
            "DocField",
            filters={
                "fieldtype": ("in", ["Table", "Table MultiSelect"]),
                "options": child_doctype,
            },
            fields=["parent", "fieldname"],
        )

        readable = []
        for parent_field in parent_fields:
            parent_doctype = parent_field.parent
            if not parent_doctype or not frappe.db.exists("DocType", parent_doctype):
                continue

            parent_meta = frappe.get_meta(parent_doctype)
            if getattr(parent_meta, "istable", False):
                readable.extend(self._readable_parent_doctypes(parent_doctype, seen.copy()))
                continue

            if not frappe.permissions.has_permission(parent_doctype, ptype="read", user=self.user):
                continue
            if not self.check_field_access(parent_doctype, parent_field.fieldname, "read"):
                continue
            readable.append(parent_doctype)

        unique = sorted(set(readable))
        self._child_parent_cache[cache_key] = unique
        return unique

    def _is_child_table(self, doc_type):
        try:
            return bool(getattr(frappe.get_meta(doc_type), "istable", False))
        except Exception:
            return False

    def get_allowed_fields(self, doc_type):
        """Get fields the user can access for a DocType."""
        meta = frappe.get_meta(doc_type)
        allowed = []
        for field in meta.fields:
            if field.hidden:
                continue
            if field.permlevel and field.permlevel > 0:
                if not self._has_permlevel(doc_type, field.permlevel, "read"):
                    continue
            allowed.append(field)
        return allowed

    def _has_permlevel(self, doc_type, permlevel, perm_type):
        """Check permlevel access."""
        perms = frappe.get_all(
            "DocPerm",
            filters={"parent": doc_type, "permlevel": permlevel},
            fields=["role", perm_type],
        )
        for p in perms:
            if p.get(perm_type) and p.role in self.roles:
                return True
        return False

    def check_field_access(self, doc_type, fieldname, perm_type="read"):
        """Check if user can access a specific field."""
        standard_fields = {
            "name", "owner", "creation", "modified", "modified_by",
            "docstatus", "idx", "parent", "parenttype", "parentfield",
        }
        if fieldname in standard_fields:
            return perm_type == "read" or self.has_doctype_permission(doc_type, perm_type)

        meta = frappe.get_meta(doc_type)
        field = meta.get_field(fieldname)
        if not field:
            return False
        if field.hidden or field.read_only:
            if perm_type == "write":
                return False
        if field.permlevel and field.permlevel > 0:
            return self._has_permlevel(doc_type, field.permlevel, perm_type)
        return True

    def validate_user_identity(self):
        """Ensure user identity is from session, not from model args."""
        return frappe.session.user

    def snapshot_hash(self):
        """Return a hash of the permission snapshot for audit."""
        import hashlib
        import json
        data = {
            "user": self.user,
            "roles": sorted(self.roles),
            "company": self.company,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
