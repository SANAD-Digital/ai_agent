"""HTTP metadata adjustments that make Frappe OAuth resource-aware for MCP."""

import json

import frappe

from ai_agent.mcp.api import MCP_ENDPOINT


def after_request(response, request):
    """Advertise the exact MCP resource without changing other Frappe APIs."""
    origin = f"{request.scheme}://{request.host}"
    metadata_path = f"/.well-known/oauth-protected-resource{MCP_ENDPOINT}"

    if request.path == MCP_ENDPOINT and response.status_code in (401, 403):
        # Frappe answers an unauthenticated call with 403, but MCP clients
        # (Claude, ChatGPT) only start the OAuth flow on 401.
        if getattr(frappe.session, "user", "Guest") == "Guest":
            response.status_code = 401
        value = f'Bearer resource_metadata="{origin}{metadata_path}"'
        # Frappe adds its origin-level challenge after after_request hooks.
        # response_headers is applied last, so the MCP-specific resource wins.
        frappe.local.response_headers["WWW-Authenticate"] = value
        response.headers["WWW-Authenticate"] = value
        return

    if request.method != "GET" or request.path != metadata_path or response.status_code != 200:
        return
    try:
        payload = json.loads(response.get_data(as_text=True))
    except (TypeError, ValueError):
        return
    payload["resource"] = f"{origin}{MCP_ENDPOINT}"
    response.set_data(json.dumps(payload, ensure_ascii=False))
