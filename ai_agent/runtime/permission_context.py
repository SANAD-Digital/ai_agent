import frappe


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
        try:
            return frappe.permissions.has_permission(
                doc_type,
                ptype=perm_type,
                user=self.user,
            )
        except frappe.PermissionError:
            # Some Single DocTypes (e.g. System Health Report) raise instead of
            # returning False when their controller loads for a non-manager.
            return False

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
            # Fails closed: a non-child DocType goes through the regular has_permission check.
            return False

    def _has_permlevel(self, doc_type, permlevel, perm_type, parent_doctype=None):
        """Check permlevel access for this context's user (DocPerm + Custom DocPerm)."""
        meta = frappe.get_meta(doc_type)
        if meta.istable and not parent_doctype:
            return False
        return permlevel in meta.get_permlevel_access(
            permission_type=perm_type, parenttype=parent_doctype, user=self.user
        )

    def check_field_access(self, doc_type, fieldname, perm_type="read", parent_doctype=None):
        """Check if user can access a specific field.

        Child-table fields take their permlevel rules from the parent DocType,
        so ``parent_doctype`` must be given for them.
        """
        standard_fields = {
            "name", "owner", "creation", "modified", "modified_by",
            "docstatus", "idx", "parent", "parenttype", "parentfield",
        }
        if fieldname in standard_fields:
            # System fields are never writable through the agent.
            return perm_type == "read"

        meta = frappe.get_meta(doc_type)
        field = meta.get_field(fieldname)
        if not field:
            return False
        if field.fieldtype in ("Password", "Password Hash"):
            return False
        if field.hidden or field.read_only:
            if perm_type == "write":
                return False
        if meta.istable and not parent_doctype:
            # Without the parent we cannot evaluate permlevels; only level 0 is safe.
            return not field.permlevel
        if field.permlevel and field.permlevel > 0 or meta.istable:
            return self._has_permlevel(doc_type, field.permlevel or 0, perm_type, parent_doctype)
        return True

    def can_access_document(self, doc, perm_type="read"):
        """Row-level check on a loaded document: role perms, User Permissions,
        if_owner, sharing and permission hooks, evaluated for this user."""
        return bool(
            frappe.has_permission(doc.doctype, ptype=perm_type, doc=doc, user=self.user)
        )


