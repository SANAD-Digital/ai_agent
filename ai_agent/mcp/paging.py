"""Signed page tokens: the embedded table asks for the next page / sort / search without the LLM."""

import base64
import copy
import hashlib
import hmac
import json


from ai_agent.mcp.approvals import ApprovalUiError, _secret

MAX_PAGE = 200
PAGEABLE_TOOLS = {"data.query"}
FILTER_OPERATORS = {"=", "!=", ">=", "<=", "like", "in"}
MAX_FILTERS = 8


def issue(tool, arguments, user):
    body = base64.urlsafe_b64encode(json.dumps({"t": tool, "a": arguments, "u": user}, default=str, sort_keys=True).encode()).decode()
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read(token, user):
    try:
        body, sig = str(token).rsplit(".", 1)
        expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, sig):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        raise ApprovalUiError("رمز الصفحة غير صالح.")
    if data.get("u") != user or data.get("t") not in PAGEABLE_TOOLS:
        raise ApprovalUiError("رمز الصفحة غير صالح.")
    return data["t"], data["a"]


def page_arguments(arguments, *, offset=0, limit=None, sort_key=None, sort_dir="asc", search_key=None, search=None, filters=None):
    """Same query, another window. Every change is re-validated by the query validator."""
    args = copy.deepcopy(arguments)
    spec = args["specification"] if isinstance(args.get("specification"), dict) else args
    spec["offset"] = max(0, int(offset or 0))
    spec["limit"] = max(1, min(int(limit or spec.get("limit") or 50), MAX_PAGE))
    if sort_key:
        spec["order_by"] = [{"field": str(sort_key), "direction": "desc" if sort_dir == "desc" else "asc"}]
    if search and search_key:
        text = str(search).strip().replace("%", "")[:80]
        if text:
            spec["filters"] = list(spec.get("filters") or []) + [{"field": str(search_key), "operator": "like", "value": f"%{text}%"}]
    for flt in (filters or [])[:MAX_FILTERS]:
        # Only simple comparisons from the filter bar; the query validator still checks field and value.
        if isinstance(flt, dict) and flt.get("operator") in FILTER_OPERATORS and isinstance(flt.get("field"), str):
            spec["filters"] = list(spec.get("filters") or []) + [{"field": flt["field"], "operator": flt["operator"], "value": flt.get("value")}]
    return args
