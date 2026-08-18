import frappe


class V15Adapter:
    """Query adapter for Frappe v15 using frappe.get_list."""

    def execute(self, spec, permission_context):
        doc_type = spec["doctype"]
        fields = self._build_fields(spec.get("fields", []))
        filters = self._build_filters(spec.get("filters", []))
        order_by = self._build_order_by(spec.get("order_by", []))
        limit = spec.get("limit", 500)
        offset = spec.get("offset", 0)

        results = frappe.get_list(
            doc_type,
            fields=fields,
            filters=filters,
            order_by=order_by,
            limit_page_length=limit,
            limit_start=offset,
            as_dict=True,
            ignore_permissions=False,
        )
        return {"rows": results, "row_count": len(results)}

    def _build_fields(self, fields):
        result = []
        for f in fields:
            if isinstance(f, str):
                result.append(f)
            elif isinstance(f, dict):
                fieldname = f.get("field", "")
                aggregate = f.get("aggregate")
                alias = f.get("alias")
                if aggregate:
                    expr = f"`${aggregate}`(`{fieldname}`)"
                    if alias:
                        expr += f" as `{alias}`"
                    result.append(expr)
                elif alias:
                    result.append(f"`{fieldname}` as `{alias}`")
                else:
                    result.append(fieldname)
        return result

    def _build_filters(self, filters):
        result = []
        for f in filters:
            op = f.get("operator")
            val = f.get("value")
            field = f.get("field")
            if op == "between" and isinstance(val, list) and len(val) == 2:
                result.append([field, "between", val])
            elif op in ("in", "not in"):
                result.append([field, op, val])
            elif op == "is set":
                result.append([field, "is", "set"])
            elif op == "is not set":
                result.append([field, "is", "not set"])
            else:
                result.append([field, op, val])
        return result

    def _build_order_by(self, order_by):
        parts = []
        for ob in order_by:
            field = ob.get("field", "")
            direction = ob.get("direction", "asc").upper()
            parts.append(f"{field} {direction}")
        return ", ".join(parts) if parts else ""
