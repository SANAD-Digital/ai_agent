"""Authenticated Streamable HTTP endpoint for Model Context Protocol clients."""

import json
from urllib.parse import quote, unquote, urlparse

import frappe
from werkzeug.wrappers import Response

from ai_agent.mcp.protocol import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    MCP_PROTOCOL_VERSION,
    MCPProtocolError,
    dispatch,
    error_response,
)
from ai_agent.mcp.service import MCPService


MCP_ENDPOINT = "/api/method/ai_agent.mcp.api.handle"
NAMED_TARGETS = {"resources/read": "uri", "prompts/get": "name"}


@frappe.whitelist(methods=["POST", "GET", "DELETE"])
def handle():
    """Serve one stateless MCP JSON-RPC request over Streamable HTTP.

    Frappe authenticates the request before this function runs. OAuth bearer
    tokens, API tokens, and authenticated sessions therefore resolve to the
    same Frappe user and permission model.

    Only POST carries JSON-RPC. Streamable HTTP clients also open a GET for a
    server-to-client event stream and may DELETE to end a session; this server
    is stateless and offers neither, which the transport spec expresses as
    405 (a 403 there makes client SDKs report the connection as failed).
    """
    if frappe.request.method != "POST":
        response = Response(status=405)
        response.headers["Allow"] = "POST"
        return response
    request_id = None
    method = None
    try:
        _validate_origin(frappe.request)
        payload = frappe.request.get_json(silent=True)
        if isinstance(payload, dict):
            request_id = payload.get("id")
            method = payload.get("method")
        if payload is None:
            raise MCPProtocolError(-32700, "Invalid or missing JSON body")
        if isinstance(payload, list):
            raise MCPProtocolError(-32600, "JSON-RPC batch requests are not supported")

        _trace("request_received", method)
        protocol_version = _validate_transport(frappe.request.headers, payload)
        result = dispatch(payload, MCPService(), protocol_version=protocol_version)
        if result is None:
            return Response(status=202)
        if isinstance(result, dict) and result.get("error"):
            _trace("request_failed", method, code=result["error"].get("code"), error=result["error"].get("message"))
        return _json_response(result, protocol_version=_response_version(method, result, protocol_version))
    except MCPProtocolError as exc:
        if exc.code == -32000:
            status = 429
        else:
            status = 400 if exc.code in (-32020, -32022, -32700, -32600) else 200
        _trace("request_failed", method, code=exc.code, error=exc.message, http_status=status)
        return _json_response(error_response(request_id, exc.code, exc.message, exc.data), status=status)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "MCP Request Failed")
        _trace("request_failed", method, code=-32603, error="Internal server error")
        return _json_response(error_response(request_id, -32603, "Internal server error"))


def _response_version(method, result, header_version):
    """The version the response header states must be the one in force for this exchange: the negotiated one on
    `initialize`, otherwise the one the client sent. A client that sent none gets none back (never a newer
    version than it negotiated)."""
    if method == "initialize" and isinstance(result, dict):
        return (result.get("result") or {}).get("protocolVersion") or header_version
    if method == "server/discover":
        return MCP_PROTOCOL_VERSION
    return header_version


def _trace(event, method, **extra):
    """Every request a host makes leaves a line, including the ones refused before they reach a tool."""
    from ai_agent.mcp import events

    headers = frappe.request.headers
    events.log(event, method=method, protocol_header=headers.get("MCP-Protocol-Version"), accept=(headers.get("Accept") or "")[:60],
               origin=headers.get("Origin"), mcp_name=(headers.get("Mcp-Name") or None) and headers.get("Mcp-Name")[:120], **extra)


def _validate_origin(request):
    """Reject browser cross-origin calls to limit DNS-rebinding attacks."""
    origin = request.headers.get("Origin")
    if not origin:
        return
    parsed = urlparse(origin)
    request_origin = f"{request.scheme}://{request.host}"
    allowed = {
        value.strip().rstrip("/")
        for value in (
            frappe.get_cached_value("AI Settings", "AI Settings", "mcp_allowed_origins") or ""
        ).splitlines()
        if value.strip()
    }
    if not parsed.scheme or origin.rstrip("/") not in {request_origin, *allowed}:
        raise MCPProtocolError(-32003, "Cross-origin MCP requests are not allowed")


def _validate_transport(headers, payload):
    """Validate both the 2026 stateless envelope and legacy handshake traffic."""
    header_version = headers.get("MCP-Protocol-Version")
    if not header_version:
        return None
    if header_version in HANDSHAKE_PROTOCOL_VERSIONS:
        return header_version
    if header_version != MCP_PROTOCOL_VERSION:
        raise MCPProtocolError(
            -32022,
            "Unsupported MCP protocol version",
            {"supported": [MCP_PROTOCOL_VERSION, *sorted(HANDSHAKE_PROTOCOL_VERSIONS)]},
        )

    method = payload.get("method")
    if headers.get("Mcp-Method") != method:
        raise MCPProtocolError(-32020, "Mcp-Method header does not match the JSON-RPC method")

    raw_params = payload.get("params")
    params = {} if raw_params is None else raw_params
    if not isinstance(params, dict):
        raise MCPProtocolError(-32020, "Request params must be an object")
    # Mcp-Name names the target of the request so intermediaries can route without reading the body:
    # the tool for tools/call, the resource URI for resources/read, the prompt for prompts/get.
    actual_name = headers.get("Mcp-Name")
    if method == "tools/call":
        if params.get("name") and actual_name != params.get("name"):
            raise MCPProtocolError(-32020, "Mcp-Name header does not match the requested tool")
    elif actual_name and method in NAMED_TARGETS:
        expected = params.get(NAMED_TARGETS[method])
        if expected and actual_name not in (expected, quote(str(expected), safe=""), quote(str(expected), safe=":/")) and unquote(actual_name) != expected:
            raise MCPProtocolError(-32020, f"Mcp-Name header does not match the requested {NAMED_TARGETS[method]}")
    # On any other method the header carries nothing we act on; the body is authoritative.

    meta = params.get("_meta") or {}
    if meta.get("io.modelcontextprotocol/protocolVersion") != MCP_PROTOCOL_VERSION:
        raise MCPProtocolError(-32020, "Request _meta protocol version does not match the header")
    if not isinstance(meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
        raise MCPProtocolError(-32020, "Request _meta must include client capabilities")
    return MCP_PROTOCOL_VERSION


def _json_response(payload, status=200, protocol_version=None):
    response = Response(
        json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
    )
    if protocol_version:
        response.headers["MCP-Protocol-Version"] = protocol_version
    response.headers["Cache-Control"] = "no-store"
    return response


@frappe.whitelist()
def diagnostics():
    """What each MCP host sent and did (System Manager only): protocol, capabilities, resources read, tools called."""
    frappe.only_for("System Manager")
    from ai_agent.mcp import diagnostics as store
    from ai_agent.mcp import events as ev

    return {"hosts": store.snapshot(), "events": ev.recent(200), "log_file": ev.log_path()}
