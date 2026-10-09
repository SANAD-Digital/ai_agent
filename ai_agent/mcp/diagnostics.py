"""What each MCP host actually sends and does, kept for a week so a failing host can be diagnosed with facts.

Nothing sensitive is stored: method names, protocol version, client name/version, capability *keys*, user agent,
which UI resources were read. A host that reads our UI resource is, by that fact, rendering MCP Apps views.
"""

import frappe

KEY = "ai-agent-mcp-profiles"
TTL = 7 * 86400
MAX_TOOLS = 25


def _load():
    return frappe.cache.get_value(KEY) or {}


def _save(profiles):
    frappe.cache.set_value(KEY, profiles, expires_in_sec=TTL)


HANDSHAKE_METHODS = {"initialize", "server/discover", "tools/list", "resources/list", "resources/read", "resources/templates/list"}


def _declared(params):
    """Capability facts of one request (keys only, plus what the host says about MCP Apps)."""
    caps = params.get("capabilities") if isinstance(params.get("capabilities"), dict) else (params.get("_meta") or {}).get("io.modelcontextprotocol/clientCapabilities")
    if not isinstance(caps, dict):
        return {}
    extensions = caps.get("extensions") or {}
    return {"capability_keys": sorted(caps), "extensions": sorted(extensions), "ui_extension": extensions.get("io.modelcontextprotocol/ui"),
            "client": (params.get("clientInfo") or {}).get("name"), "client_version": (params.get("clientInfo") or {}).get("version"),
            "protocol": params.get("protocolVersion")}


def note_request(user, method, params, headers=None):
    if method in HANDSHAKE_METHODS:
        from ai_agent.mcp import events

        events.log("host_seen", user=user, method=method, protocol_header=(headers or {}).get("Mcp-Protocol-Version") or (headers or {}).get("MCP-Protocol-Version"),
                   **{k: v for k, v in _declared(params).items() if v is not None})
    profiles = _load()
    p = profiles.setdefault(user, {"first_seen": str(frappe.utils.now_datetime()), "methods": {}, "tool_calls": [], "resources_read": []})
    p["last_seen"] = str(frappe.utils.now_datetime())
    p["methods"][method] = p["methods"].get(method, 0) + 1
    headers = headers or {}
    p["user_agent"] = (headers.get("User-Agent") or "")[:160]
    p["origin"] = headers.get("Origin")
    p["protocol_header"] = headers.get("MCP-Protocol-Version")
    if method == "initialize":
        caps = params.get("capabilities") or {}
        p["client_info"] = params.get("clientInfo")
        p["requested_protocol"] = params.get("protocolVersion")
        p["capability_keys"] = sorted(caps) if isinstance(caps, dict) else []
        p["extensions"] = sorted((caps.get("extensions") or {})) if isinstance(caps, dict) else []
        p["experimental"] = sorted((caps.get("experimental") or {})) if isinstance(caps, dict) else []
        p["ui_extension"] = (caps.get("extensions") or {}).get("io.modelcontextprotocol/ui") if isinstance(caps, dict) else None
    meta_caps = (params.get("_meta") or {}).get("io.modelcontextprotocol/clientCapabilities") if isinstance(params, dict) else None
    if isinstance(meta_caps, dict):
        p["request_capability_keys"] = sorted(meta_caps)
        p["request_extensions"] = sorted(meta_caps.get("extensions") or {})
        p["request_ui_extension"] = (meta_caps.get("extensions") or {}).get("io.modelcontextprotocol/ui")
    if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
        p["request_meta_keys"] = sorted(params["_meta"])[:20]
    if method == "tools/call" and isinstance(params, dict):
        p["tool_calls"] = (p["tool_calls"] + [params.get("name")])[-MAX_TOOLS:]
    _save(profiles)


def note_resource_read(user, uri):
    profiles = _load()
    p = profiles.setdefault(user, {"first_seen": str(frappe.utils.now_datetime()), "methods": {}, "tool_calls": [], "resources_read": []})
    p["resources_read"] = (p["resources_read"] + [uri])[-10:]
    p["last_ui_resource_read"] = str(frappe.utils.now_datetime())
    _save(profiles)


def render_seen(user):
    """True once this user's host has fetched a UI resource: it renders MCP Apps views, whatever it declared."""
    return bool(_load().get(user, {}).get("resources_read"))


def snapshot():
    return _load()
