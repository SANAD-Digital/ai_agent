"""Authenticated Streamable HTTP endpoint for Model Context Protocol clients."""

import json
from urllib.parse import urlparse

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


@frappe.whitelist(methods=["POST"])
def handle():
    """Serve one stateless MCP JSON-RPC request over Streamable HTTP.

    Frappe authenticates the request before this function runs. OAuth bearer
    tokens, API tokens, and authenticated sessions therefore resolve to the
    same Frappe user and permission model.
    """
    request_id = None
    try:
        _validate_origin(frappe.request)
        payload = frappe.request.get_json(silent=True)
        if isinstance(payload, dict):
            request_id = payload.get("id")
        if payload is None:
            raise MCPProtocolError(-32700, "Invalid or missing JSON body")
        if isinstance(payload, list):
            raise MCPProtocolError(-32600, "JSON-RPC batch requests are not supported")

        protocol_version = _validate_transport(frappe.request.headers, payload)
        result = dispatch(payload, MCPService(), protocol_version=protocol_version)
        if result is None:
            return Response(status=202)
        return _json_response(result, protocol_version=protocol_version)
    except MCPProtocolError as exc:
        if exc.code == -32000:
            status = 429
        else:
            status = 400 if exc.code in (-32020, -32022, -32700, -32600) else 200
        return _json_response(error_response(request_id, exc.code, exc.message, exc.data), status=status)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "MCP Request Failed")
        return _json_response(error_response(request_id, -32603, "Internal server error"))


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
    expected_name = params.get("name") if method == "tools/call" else None
    actual_name = headers.get("Mcp-Name")
    if expected_name and actual_name != expected_name:
        raise MCPProtocolError(-32020, "Mcp-Name header does not match the requested tool")
    if not expected_name and actual_name:
        raise MCPProtocolError(-32020, "Mcp-Name is not valid for this request")

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
    response.headers["MCP-Protocol-Version"] = protocol_version or MCP_PROTOCOL_VERSION
    response.headers["Cache-Control"] = "no-store"
    return response
