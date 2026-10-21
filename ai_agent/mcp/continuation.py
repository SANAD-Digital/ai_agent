"""The original goal survives an approval.

A request that needs prerequisites ("create the invoice" when the customer and item do not exist yet) can carry the
next step with it (``then``). It is stored on the approval as an internal comment and, once the prerequisites are
approved and executed, the server prepares the next step itself under the same user and the same checks, so the task
does not stop at "customer created". Placeholders such as ``$created.Customer`` name the records that were just
created. Each continuation runs once.
"""

import json
import re

import frappe

from ai_agent.presentation.i18n import t

# Only requests that themselves go through the approval / permission path can be chained.
ALLOWED_TOOLS = {"actions__request_create_draft", "actions__request_bulk_create"}
MARK = "ai-agent-continuation"
MAX_BYTES = 20_000
PLACEHOLDER = re.compile(r"^\$created\.(?P<doctype>[A-Za-z][A-Za-z ]*?)(?:\.(?P<index>\d+))?$")
_COMMENT = re.compile(r"<!--" + MARK + r":(?P<json>.*?)-->", re.S)


class ContinuationError(Exception):
    pass


def clean(then):
    """Validate the model-supplied next step. Raises ContinuationError with a message the model can act on."""
    if not isinstance(then, dict):
        raise ContinuationError("`then` must be an object {goal, tool, arguments}.")
    tool = str(then.get("tool") or "").replace(".", "__")
    goal = str(then.get("goal") or "").strip()[:300]
    arguments = then.get("arguments")
    if tool not in ALLOWED_TOOLS:
        raise ContinuationError(f"`then.tool` must be one of: {', '.join(sorted(ALLOWED_TOOLS))}.")
    if not goal:
        raise ContinuationError("`then.goal` is required: say in one sentence what the user ultimately wants.")
    if not isinstance(arguments, dict) or len(json.dumps(arguments, default=str)) > MAX_BYTES:
        raise ContinuationError("`then.arguments` must be an object of reasonable size.")
    return {"goal": goal, "tool": tool, "arguments": arguments, "consumed": False}


def store(approval_name, user, then):
    data = json.dumps(then, ensure_ascii=False).replace("-->", "--\\u003e")
    frappe.get_doc({
        "doctype": "Comment", "comment_type": "Info", "reference_doctype": "AI Approval Request", "reference_name": approval_name,
        "comment_email": user, "content": t("Original goal after approval: {0}", then["goal"]) + f"\n<!--{MARK}:{data}-->",
    }).insert(ignore_permissions=True)


def _find(approval_name):
    for row in frappe.get_all("Comment", filters={"reference_doctype": "AI Approval Request", "reference_name": approval_name,
                                                   "content": ["like", f"%{MARK}:%"]}, fields=["name", "content"], order_by="creation desc", limit=1):
        match = _COMMENT.search(row.content or "")
        if match:
            return row.name, json.loads(match.group("json"))
    return None, None


def peek(approval_name):
    """The pending next step of an approval, or None."""
    _, data = _find(approval_name)
    return data if data and not data.get("consumed") else None


def take(approval_name):
    """Claim the next step exactly once. Marks it consumed before it runs so a repeated approval cannot run it twice."""
    # Two concurrent approvals (double click, host retry) both read "not consumed": only the one that sets
    # this key first may take the step.
    cache = frappe.cache()
    if not cache.set(cache.make_key(f"ai_agent:continuation:{approval_name}"), 1, ex=600, nx=True):
        return None
    name, data = _find(approval_name)
    if not data or data.get("consumed"):
        return None
    marked = dict(data, consumed=True)
    frappe.db.set_value("Comment", name, "content",
                        t("Original goal after approval: {0} (done)", data["goal"]) + f"\n<!--{MARK}:{json.dumps(marked, ensure_ascii=False).replace('-->', '--\\u003e')}-->",
                        update_modified=False)
    frappe.db.commit()
    return data


def created_documents(result):
    """[{doctype, document_name}] from an execution result (single or bulk)."""
    if not isinstance(result, dict):
        return []
    if result.get("documents"):
        return [{"doctype": d.get("doctype") or result.get("doctype"), "document_name": d.get("document_name")} for d in result["documents"] if isinstance(d, dict)]
    if result.get("document_name"):
        return [{"doctype": result.get("doctype"), "document_name": result["document_name"]}]
    return []


def resolve(value, created):
    """Replace `$created.<DocType>[.<n>]` placeholders with the names of the records just created."""
    if isinstance(value, dict):
        return {k: resolve(v, created) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, created) for v in value]
    if isinstance(value, str):
        match = PLACEHOLDER.match(value.strip())
        if match:
            docs = [d["document_name"] for d in created if d["doctype"] == match.group("doctype").strip()]
            index = int(match.group("index") or 0)
            if index >= len(docs):
                raise ContinuationError(f"Nothing of type {match.group('doctype')} was created, so {value} cannot be resolved.")
            return docs[index]
    return value
