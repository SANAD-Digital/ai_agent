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


class TestStatelessTransportHeaders(TestCase):
	"""What ChatGPT sends (protocol 2026-07-28): Mcp-Method on every request, Mcp-Name naming the tool or the resource."""

	META = {"io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION, "io.modelcontextprotocol/clientCapabilities": {"extensions": {}}}

	def _headers(self, method, name=None):
		headers = {"MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "Mcp-Method": method}
		if name is not None:
			headers["Mcp-Name"] = name
		return headers

	def _payload(self, method, **params):
		return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": self.META}}

	def test_resource_read_may_name_its_uri(self):
		uri = "ui://ai-agent/main/bb84a5d064"
		for name in (uri, "ui%3A%2F%2Fai-agent%2Fmain%2Fbb84a5d064", None):
			self.assertEqual(_validate_transport(self._headers("resources/read", name), self._payload("resources/read", uri=uri)), MCP_PROTOCOL_VERSION)

	def test_resource_read_naming_another_uri_is_refused(self):
		from ai_agent.mcp.protocol import MCPProtocolError

		with self.assertRaises(MCPProtocolError):
			_validate_transport(self._headers("resources/read", "ui://other/view"), self._payload("resources/read", uri="ui://ai-agent/main"))

	def test_tool_call_must_name_its_tool(self):
		from ai_agent.mcp.protocol import MCPProtocolError

		self.assertEqual(_validate_transport(self._headers("tools/call", "app__probe"), self._payload("tools/call", name="app__probe", arguments={})), MCP_PROTOCOL_VERSION)
		for wrong in ("data__query", None):
			with self.assertRaises(MCPProtocolError):
				_validate_transport(self._headers("tools/call", wrong), self._payload("tools/call", name="app__probe", arguments={}))

	def test_a_name_on_a_listing_request_is_ignored(self):
		for method in ("tools/list", "resources/list", "server/discover"):
			self.assertEqual(_validate_transport(self._headers(method, "anything"), self._payload(method)), MCP_PROTOCOL_VERSION)


class TestCacheableResults(TestCase):
	"""Protocol 2026-07-28: every cacheable result carries ttlMs and cacheScope, or the client rejects it
	(ChatGPT answered 404 for the view template while our resources/read lacked them)."""

	def _call(self, method, params=None, version=MCP_PROTOCOL_VERSION):
		service = Mock()
		service.instructions.return_value = "i"
		service.list_tools.return_value = []
		return dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, service, protocol_version=version)["result"]

	def test_modern_results_carry_the_required_cache_fields(self):
		from ai_agent.mcp import hosts

		for method, params in (("server/discover", None), ("tools/list", None), ("resources/list", None), ("resources/templates/list", None),
				("resources/read", {"uri": hosts.minimal_uri()})):
			result = self._call(method, params)
			self.assertEqual((result.get("ttlMs"), result.get("cacheScope"), result.get("resultType")), (0, "private", "complete"), method)

	def test_tool_results_are_not_cacheable(self):
		service = Mock()
		service.call_tool.return_value = {"content": [], "isError": False}
		result = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x", "arguments": {}}}, service, protocol_version=MCP_PROTOCOL_VERSION)["result"]
		self.assertNotIn("ttlMs", result)
		self.assertEqual(result["resultType"], "complete")

	def test_legacy_results_stay_untouched(self):
		from ai_agent.mcp import hosts

		for method, params in (("tools/list", None), ("resources/read", {"uri": hosts.minimal_uri()})):
			result = self._call(method, params, version=LEGACY_PROTOCOL_VERSION)
			for key in ("ttlMs", "cacheScope", "resultType"):
				self.assertNotIn(key, result, method)
