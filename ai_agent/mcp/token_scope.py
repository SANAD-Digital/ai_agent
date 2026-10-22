"""Confine the OAuth tokens of MCP connectors (Claude, ChatGPT, ...) to the MCP endpoint.

Frappe checks a bearer token only against its own scopes, and dynamically registered clients get "all": a token
handed to an external AI host would otherwise open /api/resource and every whitelisted method, skipping the
approval cards, masking and the code/access DocType block that the MCP tools enforce.

An MCP client is one registered through dynamic client registration (owner Guest), or one whose redirect URI
belongs to a known AI host. Registered by hand for another integration, a client keeps the full API.
"""

import frappe

from ai_agent.mcp.api import MCP_ENDPOINT

MCP_HOST_CALLBACKS = ("claude.ai", "claude.com", "anthropic.com", "chatgpt.com", "openai.com")
# What an MCP token may call: the MCP endpoint itself and the OAuth/OIDC endpoints a host uses for the token.
ALLOWED_PREFIXES = (MCP_ENDPOINT, "/api/method/frappe.integrations.oauth2.", "/.well-known/")
CACHE_KEY = "ai_agent:mcp_oauth_client"


def is_mcp_client(client):
    """Cached per client: registration source and redirect URIs do not change during a token's life."""
    if not client:
        return False
    cache = frappe.cache
    known = cache.hget(CACHE_KEY, client)
    if known is not None:
        return bool(known)
    row = frappe.db.get_value("OAuth Client", client, ["owner", "redirect_uris", "default_redirect_uri"], as_dict=True)
    uris = " ".join(filter(None, [(row or {}).get("redirect_uris"), (row or {}).get("default_redirect_uri")])).lower()
    result = bool(row) and (row.owner == "Guest" or any(host in uris for host in MCP_HOST_CALLBACKS))
    cache.hset(CACHE_KEY, client, int(result))
    return result


def restrict_mcp_tokens():
    """auth_hooks: runs after Frappe accepted the bearer token and set the user."""
    header = frappe.get_request_header("Authorization", "") or ""
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or frappe.session.user in ("", "Guest"):
        return
    path = getattr(frappe.request, "path", "") or ""
    if path.startswith(ALLOWED_PREFIXES):
        return
    client = frappe.db.get_value("OAuth Bearer Token", parts[1], "client")
    if is_mcp_client(client):
        frappe.set_user("Guest")
        raise frappe.PermissionError("This token belongs to an AI connector and works only with the MCP endpoint.")


def clear_cache(doc=None, method=None):
    frappe.cache.delete_value(CACHE_KEY)
