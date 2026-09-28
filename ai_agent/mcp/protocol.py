"""Small, dependency-free MCP Streamable HTTP protocol adapter.

The transport is stateless by design: each JSON-RPC request is authenticated
by Frappe and is safe to serve from any web worker.
"""

import json


MCP_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
HANDSHAKE_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    LEGACY_PROTOCOL_VERSION,
}
SERVER_INFO = {"name": "frappe-ai-agent", "version": "0.1.0"}


class MCPProtocolError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


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

        if method == "server/discover":
            result = {
                "supportedVersions": [MCP_PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
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
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": service.instructions(),
            }
        elif method in ("notifications/initialized", "notifications/cancelled"):
            return None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": service.list_tools()}
            if protocol_version == MCP_PROTOCOL_VERSION:
                result.update({"ttlMs": 0, "cacheScope": "private"})
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
