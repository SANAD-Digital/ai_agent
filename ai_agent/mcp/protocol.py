"""Small, dependency-free MCP Streamable HTTP protocol adapter.

The transport is stateless by design: each JSON-RPC request is authenticated
by Frappe and is safe to serve from any web worker.
"""

import json
import os


MCP_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
HANDSHAKE_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    LEGACY_PROTOCOL_VERSION,
}
# Results a client may cache (protocol 2026-07-28): `ttlMs` and `cacheScope` are required on all of them.
CACHEABLE_METHODS = {"server/discover", "tools/list", "prompts/list", "resources/list", "resources/templates/list", "resources/read"}
SERVER_INFO = {"name": "frappe-ai-agent", "version": "0.2.0"}
CLIENT_CAPS_META = "io.modelcontextprotocol/clientCapabilities"
UI_FILE = os.path.join(os.path.dirname(__file__), "ui", "minimal.html")


def _client_capabilities(method, params):
    if method == "initialize":
        return params.get("capabilities")
    return (params.get("_meta") or {}).get(CLIENT_CAPS_META)


class MCPProtocolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


UI_SRC = os.path.join(os.path.dirname(UI_FILE), "src")
# Order matters: helpers first, then the bridge, components, then the entry point that starts the app.
UI_SCRIPTS = ("core.js", "icons.js", "host.js", "components.js", "chart.js", "table.js", "cards.js", "approval.js", "app.js")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


def build_main_view(lang=None):
    """Assemble the source modules into the one self-contained HTML document MCP Apps hosts render.

    The sources stay small and reviewable; the resource stays a single document with no external files,
    so it works under a CSP that allows no network origins. It is built in the viewer's language: the
    document's lang/dir and the view's own strings (AI_I18N, read by T() in core.js). The URI carries a
    hash of the document, so each language gets its own URI."""
    from ai_agent.presentation.i18n import view_strings
    from ai_agent.presentation.labels import label_language

    lang = lang or label_language()
    shell = _localize_shell(_read(UI_SRC, "shell.html"), lang)
    strings = json.dumps(view_strings(lang), ensure_ascii=False, sort_keys=True).replace("</", "<\\/")
    script = f"var AI_I18N = {strings};\n" + "\n".join(_read(UI_SRC, name) for name in UI_SCRIPTS)
    return shell.replace("/*STYLES*/", _read(UI_SRC, "styles.css")).replace("/*SCRIPT*/", script)


def _localize_shell(html, lang):
    from ai_agent.presentation.i18n import is_rtl, t

    html = html.replace('<html lang="ar" dir="rtl">', f'<html lang="{lang}" dir="{"rtl" if is_rtl(lang) else "ltr"}">', 1)
    return html.replace('aria-label="نتيجة"', f'aria-label="{t("Result")}"', 1)


def read_minimal_view():
    return _read(os.path.dirname(UI_FILE), "minimal.html")


def read_ui_resource(uri):
    from ai_agent.mcp import hosts

    base = hosts.base_of(uri)
    if base == hosts.UI_BASE_URI:
        html = build_main_view()
    elif base == hosts.UI_MINIMAL_BASE_URI:
        html = read_minimal_view()
    else:
        raise MCPProtocolError(-32002, "Resource not found", {"uri": uri})
    return {"contents": [{"uri": uri, "mimeType": hosts.UI_MIME, "text": html, "_meta": hosts.resource_meta()}]}


def list_ui_resources():
    from ai_agent.mcp import hosts

    entries = ((hosts.main_uri(), "ai-agent-view", "AI Agent View", "Portable result viewer: tables, cards, charts, approval and choice cards (RTL)."),
               (hosts.minimal_uri(), "ai-agent-minimal", "AI Agent Minimal View", "Connection check: MCP Apps handshake and one structuredContent field."))
    return {"resources": [{"uri": uri, "name": n, "title": t, "description": d, "mimeType": hosts.UI_MIME, "_meta": hosts.resource_meta()}
                          for uri, n, t, d in entries]}


def dispatch(payload, service, protocol_version=None):
    """Dispatch one MCP JSON-RPC request.

    Returns ``None`` for JSON-RPC notifications, as required by Streamable
    HTTP, and a JSON-serializable response for requests with an id.
    """
    request_id = payload.get("id") if isinstance(payload, dict) else None
    try:
        _validate_request(payload)
        method = payload["method"]
        raw_params = payload.get("params")
        params = {} if raw_params is None else raw_params
        if not isinstance(params, dict):
            raise MCPProtocolError(-32602, "params must be an object")

        if hasattr(service, "note_request"):
            service.note_request(method, params)
        if hasattr(service, "set_client_capabilities") and method in ("initialize", "tools/list", "tools/call", "server/discover"):
            service.set_client_capabilities(_client_capabilities(method, params), remember=method == "initialize")

        if method == "server/discover":
            result = {
                "supportedVersions": [MCP_PROTOCOL_VERSION],
                "capabilities": {"tools": {}, "resources": {}},
                "instructions": service.instructions(),
                "ttlMs": 0,
                "cacheScope": "private",
            }
            protocol_version = MCP_PROTOCOL_VERSION
        elif method == "initialize":
            requested = params.get("protocolVersion")
            protocol_version = requested if requested in HANDSHAKE_PROTOCOL_VERSIONS else LEGACY_PROTOCOL_VERSION
            result = {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}, "resources": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": service.instructions(),
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": service.list_tools()}
        elif method == "resources/list":
            result = list_ui_resources()
        elif method == "resources/templates/list":
            result = {"resourceTemplates": []}
        elif method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                raise MCPProtocolError(-32602, "resources/read requires a uri")
            if hasattr(service, "note_resource_requested"):
                service.note_resource_requested(uri)
            try:
                result = read_ui_resource(uri)
            except MCPProtocolError:
                raise
            except Exception as exc:
                # A broken view is a loud error, never a silent text fallback.
                if hasattr(service, "note_resource_failed"):
                    service.note_resource_failed(uri, str(exc))
                raise MCPProtocolError(-32603, f"UI resource could not be built: {exc}")
            if hasattr(service, "note_resource_read"):
                service.note_resource_read(uri)
        elif method == "tools/call":
            tool_name = params.get("name")
            raw_arguments = params.get("arguments")
            arguments = {} if raw_arguments is None else raw_arguments
            if not isinstance(tool_name, str) or not tool_name:
                raise MCPProtocolError(-32602, "tools/call requires a tool name")
            if not isinstance(arguments, dict):
                raise MCPProtocolError(-32602, "tool arguments must be an object")
            result = service.call_tool(tool_name, arguments)
        else:
            raise MCPProtocolError(-32601, f"Method not found: {method}")

        if request_id is None:
            return None
        if protocol_version == MCP_PROTOCOL_VERSION:
            if method in CACHEABLE_METHODS:
                # Required on every cacheable result in this revision; per-user data, never cached by intermediaries.
                result.setdefault("ttlMs", 0)
                result.setdefault("cacheScope", "private")
            result.setdefault("resultType", "complete")
            result.setdefault("_meta", {})["io.modelcontextprotocol/serverInfo"] = SERVER_INFO
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    except MCPProtocolError as exc:
        return error_response(request_id, exc.code, exc.message, exc.data)
    except Exception:
        # Tool/service exceptions must not leak tracebacks, SQL, or secrets.
        return error_response(request_id, -32603, "Internal server error")


def error_response(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def tool_result(value, *, is_error=False):
    text = json.dumps(value, ensure_ascii=False, default=str)
    result = {
        "content": [{"type": "text", "text": text}],
        "isError": bool(is_error),
    }
    if isinstance(value, dict):
        result["structuredContent"] = value
    return result


def _validate_request(payload):
    if not isinstance(payload, dict):
        raise MCPProtocolError(-32600, "Request must be a JSON object")
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        raise MCPProtocolError(-32600, "Invalid JSON-RPC request")
    if not payload["method"].startswith("notifications/") and "id" not in payload:
        raise MCPProtocolError(-32600, "JSON-RPC requests must include an id")
