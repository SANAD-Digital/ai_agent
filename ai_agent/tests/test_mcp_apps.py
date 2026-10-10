import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp import approvals, hosts, paging
from ai_agent.mcp.protocol import dispatch, list_ui_resources, read_ui_resource
from ai_agent.mcp.service import MCPService
from ai_agent.presentation.i18n import t
from ai_agent.presentation.registry import registered_tools


class TestMCPAppsContract(IntegrationTestCase):
	def test_capability_detection(self):
		caps = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
		self.assertTrue(hosts.ui_supported(caps))
		self.assertFalse(hosts.ui_supported({"extensions": {}}))
		self.assertFalse(hosts.ui_supported({"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/plain"]}}}))
		self.assertTrue(hosts.ui_supported({"extensions": {"io.modelcontextprotocol/ui": {}}}), "declaring the extension without MIME types still means it renders views")
		self.assertIsNone(hosts.ui_supported(None))

	def test_tool_and_resource_meta_use_only_documented_standard_keys(self):
		meta = hosts.tool_ui_meta()
		self.assertRegex(meta["ui"]["resourceUri"], r"^ui://ai-agent/main/[0-9a-f]{10}$")
		self.assertEqual(meta["ui"]["visibility"], ["model", "app"])
		self.assertEqual(meta["ui/resourceUri"], meta["ui"]["resourceUri"])
		self.assertFalse([k for k in meta if k.startswith("openai/")], "standard keys only: no vendor alias that could select another code path")
		self.assertEqual(hosts.resource_meta(), {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}}})
		app_only = hosts.tool_ui_meta(app_only=True, with_view=False)
		self.assertEqual(app_only["ui"], {"visibility": ["app"]})

	def test_resource_list_and_read(self):
		listing = list_ui_resources()["resources"][0]
		self.assertRegex(listing["uri"], r"^ui://ai-agent/main/[0-9a-f]{10}$")
		self.assertEqual(listing["mimeType"], "text/html;profile=mcp-app")
		content = read_ui_resource("ui://ai-agent/main")["contents"][0]
		self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
		self.assertIn("ui/initialize", content["text"])
		self.assertEqual(content["_meta"]["ui"]["csp"]["connectDomains"], [])
		self.assertEqual(content["_meta"]["ui"]["csp"]["resourceDomains"], [])

	def test_main_view_is_assembled_from_sources_into_one_document(self):
		html = read_ui_resource("ui://ai-agent/main")["contents"][0]["text"]
		self.assertNotIn("/*STYLES*/", html)
		self.assertNotIn("/*SCRIPT*/", html)
		self.assertEqual(html.count("<script"), 1)
		for needle in ("function Table(", "function Chart(", "function Approval(", "var Host = {", "ui/download-file", "ui/update-model-context", "ui/message"):
			self.assertIn(needle, html)

	def test_vendor_api_is_only_ever_a_guarded_fallback(self):
		html = read_ui_resource("ui://ai-agent/main")["contents"][0]["text"]
		self.assertIn("window.openai && typeof window.openai", html)  # single guarded accessor
		import re

		lines = [line for line in html.splitlines() if re.search(r"window\.openai", line)]
		self.assertEqual(len(lines), 1)  # one guarded accessor; every other use goes through Host.*, never a host check

	def test_view_has_no_external_origins(self):
		html = read_ui_resource("ui://ai-agent/main")["contents"][0]["text"]
		# The SVG namespace URI is an identifier, not a request; everything that can hit the network is banned.
		for needle in ("fetch(", "XMLHttpRequest", "WebSocket", "<script src", "<link", "@import", "sendBeacon", "EventSource"):
			self.assertNotIn(needle, html)

	def test_unknown_resource_is_an_error(self):
		response = dispatch({"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": "ui://other"}}, None)
		self.assertIn("error", response)

	def test_capabilities_advertise_resources(self):
		service = type("S", (), {"instructions": lambda self: "x"})()
		response = dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}, service)
		self.assertIn("resources", response["result"]["capabilities"])

	def test_presenter_tools_are_registered(self):
		tools = registered_tools()
		for name in ("data.query", "data.get_document", "reports.run", "metrics.query", "actions.request_create_draft"):
			self.assertIn(name, tools)
		self.assertNotIn("data.count", tools)


class TestPagingAndTokens(IntegrationTestCase):
	def test_page_token_roundtrip_and_tamper(self):
		args = {"specification": {"doctype": "Company", "limit": 10}}
		token = paging.issue("data.query", args, "a@b.c")
		self.assertEqual(paging.read(token, "a@b.c"), ("data.query", args))
		with self.assertRaises(approvals.ApprovalUiError):
			paging.read(token, "other@b.c")
		with self.assertRaises(approvals.ApprovalUiError):
			paging.read("x" + token, "a@b.c")

	def test_page_arguments_only_change_the_window(self):
		args = {"specification": {"doctype": "Company", "filters": [], "limit": 25}}
		out = paging.page_arguments(args, offset=50, sort_key="name", sort_dir="desc", search_key="name", search="SN%D")
		spec = out["specification"]
		self.assertEqual(spec["offset"], 50)
		self.assertEqual(spec["order_by"], [{"field": "name", "direction": "desc"}])
		self.assertEqual(spec["filters"][-1]["value"], "%SND%")
		self.assertEqual(args["specification"]["limit"], 25)  # original untouched

	def test_page_filters_are_whitelisted_and_capped(self):
		args = {"specification": {"doctype": "Sales Invoice", "filters": [{"field": "company", "operator": "=", "value": "X"}]}}
		out = paging.page_arguments(args, filters=[
			{"field": "status", "operator": "=", "value": "Paid"}, {"field": "posting_date", "operator": ">=", "value": "2026-09-01"},
			{"field": "x", "operator": "; drop table", "value": 1}, {"field": 5, "operator": "=", "value": 1}, "junk"])
		fields = [f["field"] for f in out["specification"]["filters"]]
		self.assertEqual(fields, ["company", "status", "posting_date"])
		many = paging.page_arguments(args, filters=[{"field": "f", "operator": "=", "value": i} for i in range(20)])
		self.assertEqual(len(many["specification"]["filters"]), 1 + paging.MAX_FILTERS)

	def test_approval_tokens_are_bound_to_action_user_and_payload(self):
		t = approvals.issue_token("APR-1", "hash", "u@x.c", "approve")
		self.assertEqual(t, approvals.issue_token("APR-1", "hash", "u@x.c", "approve"))
		for changed in (("APR-2", "hash", "u@x.c", "approve"), ("APR-1", "h2", "u@x.c", "approve"),
			("APR-1", "hash", "v@x.c", "approve"), ("APR-1", "hash", "u@x.c", "reject")):
			self.assertNotEqual(t, approvals.issue_token(*changed))

	def test_steps_show_where_a_request_stopped(self):
		pending = {s["key"]: s["state"] for s in approvals._steps("pending")}
		self.assertEqual((pending["preview"], pending["approval"], pending["execute"]), ("done", "active", "todo"))
		failed = {s["key"]: s["state"] for s in approvals._steps("failed")}
		self.assertEqual((failed["approval"], failed["execute"]), ("done", "error"))
		self.assertTrue(all(s["state"] == "done" for s in approvals._steps("verified")))
		invalid = {s["key"]: s["state"] for s in approvals._steps("pending", valid=False)}
		self.assertEqual(invalid["validate"], "error")


class TestApprovalCardWire(IntegrationTestCase):
	def setUp(self):
		# The card depends on the site's four-eyes policy; pin it so results do not depend on how the site is configured.
		patcher = patch.object(approvals, "four_eyes_enabled", return_value=False)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _approval(self):
		payload = {"doctype": "Sales Invoice", "customer": "X", "company": "Y", "items": [{"item_code": "I", "qty": 2, "rate": 5}]}
		return frappe._dict(name="APR-T", action_type="Create", doctype_name="Sales Invoice", document_name=None, status="Pending",
			payload_json=json.dumps(payload), payload_hash="abc", action_summary="Create Sales Invoice draft", impact_summary="",
			expires_at=None, run=None, requested_by="Administrator")

	def test_tokens_stay_out_of_model_visible_content(self):
		from ai_agent.mcp.service import MCPService

		svc = MCPService.__new__(MCPService)
		svc.user, svc.ui_supported = "Administrator", True
		frappe.flags.mcp_ui_supported = True
		with patch.object(approvals, "_preview", return_value={"fields": [], "items": [], "kpis": [], "warnings": [], "valid": True}), \
				patch.object(approvals, "app_approval_enabled", return_value=True):
			envelope = approvals.card_envelope(self._approval(), "Administrator")
		result = MCPService._envelope_result(svc, envelope, "actions.request_create_draft", {})
		token = approvals.issue_token("APR-T", "abc", "Administrator", "approve")
		model_visible = json.dumps(result["content"]) + json.dumps(result["structuredContent"])
		self.assertNotIn(token, model_visible)
		self.assertIn(token, json.dumps(result["_meta"]))

	def test_host_without_apps_gets_no_tokens_and_a_desk_link(self):
		frappe.flags.mcp_ui_supported = False
		with patch.object(approvals, "_preview", return_value={"fields": [], "items": [], "kpis": [], "warnings": [], "valid": True}), \
				patch.object(approvals, "app_approval_enabled", return_value=True):
			card = approvals.build_card(self._approval(), "Administrator")
		self.assertEqual(card["actions"], [])
		self.assertIn("APR-T", card["fallback"]["text"])
		frappe.flags.mcp_ui_supported = None

	def test_every_button_carries_the_ids_and_token_its_tool_needs(self):
		frappe.flags.mcp_ui_supported = True
		with patch.object(approvals, "_preview", return_value={"fields": [], "items": [], "kpis": [], "warnings": [], "valid": True}), \
				patch.object(approvals, "app_approval_enabled", return_value=True):
			card = approvals.build_card(self._approval(), "Administrator")
		by_key = {a["key"]: a for a in card["actions"]}
		self.assertEqual(set(by_key), {"approve", "edit", "cancel"})
		for key, action in (("approve", "approve"), ("cancel", "reject"), ("edit", "revise")):
			args = by_key[key]["arguments"]
			self.assertEqual(args["approval_id"], "APR-T")
			self.assertEqual(args["token"], approvals.issue_token("APR-T", "abc", "Administrator", action))
		self.assertNotEqual(by_key["approve"]["arguments"]["token"], by_key["cancel"]["arguments"]["token"])
		frappe.flags.mcp_ui_supported = None

	def test_four_eyes_hides_approve_on_your_own_request(self):
		frappe.flags.mcp_ui_supported = True
		with patch.object(approvals, "_preview", return_value={"fields": [], "items": [], "kpis": [], "warnings": [], "valid": True}), \
				patch.object(approvals, "app_approval_enabled", return_value=True), patch.object(approvals, "four_eyes_enabled", return_value=True):
			own = approvals.build_card(self._approval(), "Administrator")
			other = approvals.build_card(self._approval(), "someone@else.com")
		self.assertNotIn("approve", [a["key"] for a in own["actions"]])
		self.assertIn("cancel", [a["key"] for a in own["actions"]])
		self.assertTrue(any(w["code"] == "four_eyes" for w in own["warnings"]))
		self.assertIn("approve", [a["key"] for a in other["actions"]])
		frappe.flags.mcp_ui_supported = None

	def test_critical_warning_removes_the_approve_button(self):
		from ai_agent.presentation import schema

		frappe.flags.mcp_ui_supported = True
		preview = {"fields": [], "items": [], "kpis": [], "valid": True, "warnings": [schema.warning("critical", "المخزون غير كافٍ")]}
		with patch.object(approvals, "_preview", return_value=preview), patch.object(approvals, "app_approval_enabled", return_value=True):
			card = approvals.build_card(self._approval(), "Administrator")
		self.assertNotIn("approve", [a["key"] for a in card["actions"]])
		frappe.flags.mcp_ui_supported = None


class TestMinimalSlice(IntegrationTestCase):
	"""tool metadata -> resources/list -> resources/read -> tool result, through the real dispatcher."""

	@property
	def URI(self):
		return hosts.minimal_uri()

	def _svc(self, ui):
		from ai_agent.tests.test_mcp_service_views import make_service

		return make_service(ui)

	def _rpc(self, svc, method, params=None):
		return dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, svc)["result"]

	def test_probe_tool_links_the_minimal_resource_with_official_metadata(self):
		svc = self._svc(True)
		with patch.object(MCPService, "_is_exposed", return_value=True):
			tool = next(t for t in self._rpc(svc, "tools/list")["tools"] if t["name"] == "app__probe")
		self.assertEqual(tool["_meta"]["ui"]["resourceUri"], self.URI)
		self.assertEqual(tool["_meta"]["ui"]["visibility"], ["model", "app"])
		self.assertTrue(tool["annotations"]["readOnlyHint"])

	def test_resources_list_and_read_return_a_valid_document(self):
		svc = self._svc(True)
		listed = {r["uri"]: r for r in self._rpc(svc, "resources/list")["resources"]}
		self.assertEqual(listed[self.URI]["mimeType"], "text/html;profile=mcp-app")
		content = self._rpc(svc, "resources/read", {"uri": self.URI})["contents"][0]
		self.assertEqual(content["uri"], self.URI)
		self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
		self.assertTrue(content["text"].lstrip().lower().startswith("<!doctype html>"))
		for needle in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result", "host-context-changed"):
			self.assertIn(needle, content["text"])
		self.assertNotIn("window.openai", content["text"])
		self.assertEqual(content["_meta"]["ui"]["csp"]["connectDomains"], [])

	def test_probe_result_has_structured_content_and_standalone_text(self):
		for ui in (True, False, None):
			svc = self._svc(ui)
			result = self._rpc(svc, "tools/call", {"name": "app__probe", "arguments": {}})
			self.assertFalse(result["isError"])
			self.assertIsInstance(result["structuredContent"]["companies"], int)
			self.assertEqual(result["content"][0]["text"], t("Companies: {0}", result["structuredContent"]["companies"]))
			self.assertNotIn("_meta", {k: v for k, v in result.items() if k == "_meta" and "ai_agent/presentation" in v})


class TestHostEvidence(IntegrationTestCase):
	"""How support is decided: declaration, else proof (the host fetched a UI resource), else what it declared."""

	def _svc(self, render_seen):
		svc = MCPService.__new__(MCPService)
		svc.user = "probe@example.com"
		self._patch = patch("ai_agent.mcp.diagnostics.render_seen", return_value=render_seen)
		self._patch.start()
		self.addCleanup(self._patch.stop)
		return svc

	def test_declared_extension_wins(self):
		caps = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
		svc = self._svc(False)
		svc.set_client_capabilities(caps)
		self.assertIs(svc.ui_supported, True)

	def test_fetching_a_ui_resource_proves_rendering_even_without_a_declaration(self):
		svc = self._svc(True)
		svc.set_client_capabilities({"tools": {}})
		self.assertIs(svc.ui_supported, True)

	def test_declared_capabilities_without_mcp_apps_and_no_proof_means_text(self):
		svc = self._svc(False)
		svc.set_client_capabilities({"tools": {}})
		self.assertIs(svc.ui_supported, False)

	def test_nothing_known_is_unknown_not_false(self):
		svc = self._svc(False)
		with patch("frappe.cache") as cache:
			cache.get_value.return_value = None
			cache.make_key.side_effect = lambda k: k
			svc.set_client_capabilities(None)
		self.assertIsNone(svc.ui_supported)


class TestDiagnostics(IntegrationTestCase):
	def setUp(self):
		from ai_agent.mcp import diagnostics

		self.store = {}
		for name, fake in (("_load", lambda: self.store.get("profiles") or {}), ("_save", lambda profiles: self.store.__setitem__("profiles", profiles))):
			patcher = patch.object(diagnostics, name, side_effect=fake)
			patcher.start()
			self.addCleanup(patcher.stop)

	def test_records_what_the_host_declares_and_does_without_secrets(self):
		from ai_agent.mcp import diagnostics

		diagnostics.note_request("u@x.c", "initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "chatgpt", "version": "1"},
			"capabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}, "elicitation": {}}},
			{"User-Agent": "openai-mcp/1.0", "Authorization": "Bearer SECRET"})
		diagnostics.note_request("u@x.c", "tools/call", {"name": "data__query", "arguments": {"secret": "x"}}, {})
		diagnostics.note_resource_read("u@x.c", "ui://ai-agent/main")
		p = diagnostics.snapshot()["u@x.c"]
		self.assertEqual(p["client_info"]["name"], "chatgpt")
		self.assertEqual(p["extensions"], ["io.modelcontextprotocol/ui"])
		self.assertEqual(p["tool_calls"], ["data__query"])
		self.assertEqual(p["resources_read"], ["ui://ai-agent/main"])
		self.assertTrue(diagnostics.render_seen("u@x.c"))
		blob = json.dumps(p)
		self.assertNotIn("SECRET", blob)
		self.assertNotIn("Authorization", blob)
		self.assertNotIn('"secret"', blob)

	def test_snapshot_is_for_system_managers_only(self):
		from ai_agent.mcp import api

		with patch.object(frappe, "only_for", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				api.diagnostics()


class TestViewCaching(IntegrationTestCase):
	"""Hosts cache a view by URI: the URI must change when the view changes, and old URIs must keep working."""

	def test_uri_follows_the_content_and_old_uris_still_resolve(self):
		current = hosts.main_uri()
		self.assertRegex(current, r"^ui://ai-agent/main/[0-9a-f]{10}$")
		with patch("ai_agent.mcp.protocol.build_main_view", return_value="<!doctype html><p>changed</p>"):
			self.assertNotEqual(hosts.main_uri(), current)
		for uri in (current, "ui://ai-agent/main", "ui://ai-agent/main/0000000000"):
			self.assertIn("ui/initialize", read_ui_resource(uri)["contents"][0]["text"])
		self.assertEqual(read_ui_resource("ui://ai-agent/minimal/abc")["contents"][0]["uri"], "ui://ai-agent/minimal/abc")
		for bad in ("ui://ai-agent/mainx", "ui://other/main", "ui://ai-agent"):
			with self.assertRaises(Exception):
				read_ui_resource(bad)

	def test_listing_and_tools_name_the_same_uri(self):
		listed = {r["uri"] for r in list_ui_resources()["resources"]}
		self.assertEqual(listed, {hosts.main_uri(), hosts.minimal_uri()})
		self.assertIn(hosts.tool_ui_meta()["ui"]["resourceUri"], listed)


class TestViewLoad(IntegrationTestCase):
	"""structuredContent is the one channel every host delivers to the view: it must be enough to render everything."""

	def setUp(self):
		from ai_agent.tests.test_mcp_service_views import make_service

		self.svc = make_service(True)
		for name, fn in (("_is_exposed", lambda s, t: True), ("_create_run", lambda s, t, a: frappe._dict(name="RUN-T")), ("_finish_run", lambda s, *a: None)):
			patcher = patch.object(MCPService, name, fn)
			patcher.start()
			self.addCleanup(patcher.stop)

		def execute(gateway, name, args, **kw):
			return MCPService._execute_handler(self.svc.registry.get_tool(name), args, gateway.ctx)

		patcher = patch("ai_agent.runtime.tool_gateway.ToolGateway.authorize_and_execute", execute)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_ref_in_structured_content_loads_the_full_view(self):
		first = self.svc.call_tool("data__query", {"specification": {"doctype": "Company", "fields": ["name", "default_currency"], "limit": 20}})
		ref = first["structuredContent"]["presentation"]["ref"]
		loaded = self.svc.call_tool("view__load", {"ref": ref})
		self.assertFalse(loaded["isError"], loaded["content"])
		p = loaded["structuredContent"]["presentation"]
		self.assertTrue(p["full"])
		self.assertEqual(len(p["rows"]), len(first["_meta"]["ai_agent/presentation"]["rows"]))
		self.assertTrue(p["rows"][0]["actions"][0]["arguments"])

	def test_ref_is_bound_to_the_user_and_cannot_be_forged(self):
		from ai_agent.mcp import refs

		ref = refs.issue("tool", {"t": "data.query", "a": {"specification": {"doctype": "Company"}}}, "someone@else.com")
		self.assertTrue(self.svc.call_tool("view__load", {"ref": ref})["isError"])
		self.assertTrue(self.svc.call_tool("view__load", {"ref": "abc.def"})["isError"])
		mine = refs.issue("tool", {"t": "data.query", "a": {}}, self.svc.user)
		body, sig = mine.rsplit(".", 1)
		self.assertTrue(self.svc.call_tool("view__load", {"ref": body + "x." + sig})["isError"])

	def test_a_ref_never_repeats_a_write(self):
		from ai_agent.mcp import refs

		ref = refs.issue("tool", {"t": "actions.request_create_draft", "a": {"doctype": "Note", "payload": {"title": "x"}}}, self.svc.user)
		self.assertTrue(self.svc.call_tool("view__load", {"ref": ref})["isError"])
		self.assertIsNone(self.svc._ref({"type": "table"}, "actions.request_create_draft", {}))

	def test_host_without_apps_gets_no_ref(self):
		from ai_agent.tests.test_mcp_service_views import make_service

		plain = make_service(False).call_tool("data__query", {"specification": {"doctype": "Company", "fields": ["name"], "limit": 5}})
		self.assertNotIn("ref", plain["structuredContent"]["presentation"])


class TestResponseProtocolHeader(IntegrationTestCase):
	"""The response header must state the version in force, never a newer one than the client negotiated."""

	def test_header_follows_negotiation(self):
		from ai_agent.mcp import api

		init = {"result": {"protocolVersion": "2025-06-18"}}
		self.assertEqual(api._response_version("initialize", init, None), "2025-06-18")
		self.assertEqual(api._response_version("tools/list", {"result": {}}, "2025-11-25"), "2025-11-25")
		self.assertIsNone(api._response_version("tools/call", {"result": {}}, None))
		self.assertEqual(api._response_version("server/discover", {"result": {}}, None), "2026-07-28")
		self.assertNotIn("MCP-Protocol-Version", api._json_response({"ok": 1}).headers)
		self.assertEqual(api._json_response({"ok": 1}, protocol_version="2025-06-18").headers["MCP-Protocol-Version"], "2025-06-18")
