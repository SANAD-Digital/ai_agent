"""Signed, stateless references so the embedded view can fetch its own full presentation.

Hosts are documented to hand the view ``structuredContent``; whether a tool result's ``_meta`` also reaches the view
differs between hosts. ``structuredContent`` therefore carries a lean presentation (no rows) plus ``ref``; the view
calls the app-only tool ``view__load`` with it and receives the complete view model. A ref names either an approval
request or a read-only tool call to repeat, is bound to the user, and carries no data.
"""

import base64
import hashlib
import hmac
import json

from ai_agent.mcp.approvals import ApprovalUiError, _secret

KINDS = {"approval", "tool"}


def issue(kind, payload, user):
    body = base64.urlsafe_b64encode(json.dumps({"k": kind, "p": payload, "u": user}, default=str, sort_keys=True).encode()).decode()
    return f"{body}.{hmac.new(_secret(), ('ref:' + body).encode(), hashlib.sha256).hexdigest()[:32]}"


def read(token, user):
    try:
        body, sig = str(token).rsplit(".", 1)
        if not hmac.compare_digest(hmac.new(_secret(), ("ref:" + body).encode(), hashlib.sha256).hexdigest()[:32], sig):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        raise ApprovalUiError("مرجع العرض غير صالح.")
    if data.get("u") != user or data.get("k") not in KINDS:
        raise ApprovalUiError("مرجع العرض غير صالح.")
    return data["k"], data["p"]
