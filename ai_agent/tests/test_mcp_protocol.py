from unittest import TestCase
from unittest.mock import Mock

from ai_agent.mcp.api import _validate_transport
from ai_agent.mcp.protocol import LEGACY_PROTOCOL_VERSION, MCP_PROTOCOL_VERSION, dispatch


class TestMCPProtocol(TestCase):
    def test_initialize_negotiates_latest_legacy_protocol(self):
        service = Mock()
        service.instructions.return_value = "test instructions"
        response = dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
            },
            service,
        )
        self.assertEqual(response["result"]["protocolVersion"], LEGACY_PROTOCOL_VERSION)
        self.assertEqual(response["result"]["capabilities"]["tools"], {"listChanged": False})

    def test_tools_list_uses_service_tools(self):
        service = Mock()
        service.list_tools.return_value = [{"name": "data__query"}]
        response = dispatch(
            {"jsonrpc": "2.0", "id": "tools", "method": "tools/list", "params": {}},
            service,
        )
        self.assertEqual(response["result"], {"tools": [{"name": "data__query"}]})

    def test_tools_call_validates_arguments(self):
        service = Mock()
        response = dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "data__query", "arguments": []},
            },
            service,
        )
        self.assertEqual(response["error"]["code"], -32602)
        service.call_tool.assert_not_called()

    def test_initialized_notification_has_no_response(self):
        self.assertIsNone(
            dispatch(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                Mock(),
            )
        )

    def test_modern_discovery_result(self):
        service = Mock()
        service.instructions.return_value = "instructions"
        response = dispatch(
            {"jsonrpc": "2.0", "id": "discover", "method": "server/discover", "params": {}},
            service,
            protocol_version=MCP_PROTOCOL_VERSION,
        )
        result = response["result"]
        self.assertEqual(result["supportedVersions"], [MCP_PROTOCOL_VERSION])
        self.assertEqual(result["resultType"], "complete")
        self.assertEqual(result["cacheScope"], "private")

    def test_modern_transport_headers_must_match_body(self):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "data__count",
                "arguments": {"doctype": "Customer"},
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        headers = {
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": "tools/call",
            "Mcp-Name": "data__count",
        }
        self.assertEqual(_validate_transport(headers, payload), MCP_PROTOCOL_VERSION)
