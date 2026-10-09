"""Host adapters: the only place that knows about MCP Apps negotiation.

Standard first: one portable MCP App resource (MCP Apps spec 2026-01-26), tools linked through
``_meta.ui.resourceUri`` (plus the deprecated flat ``ui/resourceUri`` key older hosts still read).
Host-specific extras would live here as optional additions; none are needed today.
"""

# Hosts cache a view by its URI, so the URI carries the view's version: a changed view gets a new URI.
UI_BASE_URI = "ui://ai-agent/main"
UI_MINIMAL_BASE_URI = "ui://ai-agent/minimal"  # connection check: handshake + one structuredContent field


def _versioned(base, filename_or_builder):
    import hashlib

    try:
        text = filename_or_builder()
    except Exception:
        return base
    return f"{base}/{hashlib.sha256(text.encode()).hexdigest()[:10]}"


def main_uri():
    from ai_agent.mcp.protocol import build_main_view

    return _versioned(UI_BASE_URI, build_main_view)


def minimal_uri():
    from ai_agent.mcp.protocol import read_minimal_view

    return _versioned(UI_MINIMAL_BASE_URI, read_minimal_view)


def base_of(uri):
    """The view a URI names, whatever its version (older cached descriptors keep working)."""
    for base in (UI_MINIMAL_BASE_URI, UI_BASE_URI):
        if uri == base or str(uri).startswith(base + "/"):
            return base
    return None
UI_MIME = "text/html;profile=mcp-app"
UI_EXTENSION = "io.modelcontextprotocol/ui"

# Tools only the embedded app may call (drill-down, paging, approval buttons).
APP_ONLY_TOOLS = {"approvals__revise", "view__page", "view__load", "view__event"}


def ui_supported(client_capabilities):
    """True when the client advertises MCP Apps, False when it declared capabilities without it,
    None when capabilities are unknown (legacy handshake seen on a different request)."""
    if not isinstance(client_capabilities, dict):
        return None
    extension = (client_capabilities.get("extensions") or {}).get(UI_EXTENSION)
    if not isinstance(extension, dict):
        return False
    mime_types = extension.get("mimeTypes")
    # A host that lists MIME types must list ours; a host that declares the extension without listing any still renders views.
    return UI_MIME in mime_types if isinstance(mime_types, list) and mime_types else True


def tool_ui_meta(*, app_only=False, with_view=True, resource_uri=None):
    meta = {"ui": {"visibility": ["app"] if app_only else ["model", "app"]}}
    if with_view:
        uri = resource_uri or main_uri()
        meta["ui"]["resourceUri"] = uri
        meta["ui/resourceUri"] = uri  # deprecated flat key, kept for older hosts
    return meta


def resource_meta():
    """Resource-level UI metadata, kept to the keys every host documents. No network origins are declared, which is
    the secure default: the view is one self-contained document (inline script and style, system fonts)."""
    return {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}}}
