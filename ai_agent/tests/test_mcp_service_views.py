"""Tool -> presentation -> wire result, against the site's real data (read-only, no runs written)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp.service import MCPService
from ai_agent.runtime.permission_context import PermissionContext


def make_service(ui):
	svc = MCPService.__new__(MCPService)
	svc.user = "Administrator"
	svc.ui_supported = ui
	frappe.flags.mcp_ui_supported = ui
	svc.agent = frappe._dict(name="ERP Analyst", read_only=1, allow_actions=0)
	svc.company = None
	from ai_agent.runtime.tool_registry import ToolRegistry

	svc.registry = ToolRegistry()
	svc.settings = frappe._dict()
	return svc


def run_tool(svc, external_name, arguments):
	"""Run through MCPService.call_tool with run bookkeeping and gateway audit stubbed out."""
	internal = svc._internal_name(external_name)
	handler = svc.registry.get_tool(internal)

	def fake_execute(self, name, args, **kwargs):
		return MCPService._execute_handler(handler, args, PermissionContext("Administrator"))

	run = frappe._dict(name="RUN-T")
	with patch.object(MCPService, "_create_run", return_value=run), patch.object(MCPService, "_finish_run"), \
			patch("ai_agent.runtime.tool_gateway.ToolGateway.authorize_and_execute", fake_execute), \
			patch.object(MCPService, "_is_exposed", return_value=True):
		return svc.call_tool(external_name, arguments)


class TestCompanyView(IntegrationTestCase):
	QUERY = {"specification": {"doctype": "Company", "fields": ["name", "default_currency", "creation", "owner", "docstatus"], "limit": 20}}

	def test_apps_host_gets_full_presentation_in_meta_and_lite_in_structured(self):
		result = run_tool(make_service(True), "data__query", self.QUERY)
		self.assertFalse(result["isError"])
		full = result["_meta"]["ai_agent/presentation"]
		lite = result["structuredContent"]["presentation"]
		self.assertEqual(full["title"], "الشركات")
		self.assertEqual([c["key"] for c in full["columns"]][:3], ["_index", "name", "default_currency"])
		for hidden in ("owner", "creation", "docstatus"):
			self.assertNotIn(hidden, [c["key"] for c in full["columns"]])
		self.assertNotIn("rows", lite)
		self.assertTrue(lite["ref"], "the view can fetch its full model even when the host sends no _meta")
		self.assertNotIn("full", lite)
		self.assertEqual(set(result["structuredContent"]) & {"data", "summary", "presentation", "actions", "warnings", "meta"},
			{"data", "summary", "presentation", "actions", "warnings", "meta"})
		self.assertIn("| اسم الشركة |", result["content"][0]["text"])

	def test_host_without_apps_keeps_everything_in_text_and_structured(self):
		result = run_tool(make_service(False), "data__query", self.QUERY)
		self.assertNotIn("_meta", result)
		self.assertIn("الشركات", result["content"][0]["text"])
		self.assertTrue(result["structuredContent"]["summary"]["text"])

	def test_count_is_plain_text_without_widget(self):
		result = run_tool(make_service(True), "data__count", {"doctype": "Company"})
		self.assertNotIn("_meta", result)
		self.assertNotIn("presentation", result["structuredContent"])

	def test_tools_list_links_ui_only_where_a_presenter_exists(self):
		svc = make_service(True)
		with patch.object(MCPService, "_is_exposed", return_value=True), patch.object(MCPService, "_action_requests_enabled", return_value=False):
			tools = {t["name"]: t for t in svc.list_tools()}
		# Only final-result tools get a view; lookups made along the way stay text.
		self.assertRegex(tools["artifacts__create_table"]["_meta"]["ui"]["resourceUri"], r"^ui://ai-agent/main/[0-9a-f]{10}$")
		self.assertRegex(tools["artifacts__create_chart"]["_meta"]["ui"]["resourceUri"], r"^ui://ai-agent/main/[0-9a-f]{10}$")
		self.assertNotIn("_meta", tools["data__query"])
		self.assertNotIn("_meta", tools["data__count"])
		self.assertEqual(tools["view__page"]["_meta"]["ui"]["visibility"], ["app"])
		self.assertNotIn("resourceUri", tools["view__page"]["_meta"]["ui"])

	def test_document_drilldown_is_a_card_with_fields_before_technical(self):
		name = frappe.db.get_value("Company", {}, "name")
		result = run_tool(make_service(True), "data__get_document", {"doctype": "Company", "name": name})
		p = result["_meta"]["ai_agent/presentation"]
		self.assertEqual(p["type"], "document")
		self.assertTrue(p["sections"])
		self.assertIn(name, p["title"])
		shown = json.dumps([f["label"] for s in p["sections"] for f in s["fields"]], ensure_ascii=False)
		self.assertNotIn("owner", shown)


class TestFinalChartView(IntegrationTestCase):
	def test_create_chart_renders_chart_and_its_table(self):
		from ai_agent.presentation.registry import present

		args = {"title": "المبيعات الشهرية", "chart_spec": {"type": "pie", "labels": ["أ", "ب"],
			"datasets": [{"name": "المبيعات", "values": ["300", 100]}]}}
		env = present("artifacts.create_chart", args, {"artifact_name": "x", "chart_url": "/ai-chart?artifact=x"})
		p = env["presentation"]
		self.assertEqual(p["chart"]["kind"], "donut")
		self.assertEqual(p["chart"]["series"][0]["values"], [300.0, 100.0])
		self.assertEqual([r["cells"]["label"]["v"] for r in p["rows"]], ["أ", "ب"])

	def test_malformed_chart_has_no_view(self):
		from ai_agent.presentation.registry import present

		self.assertIsNone(present("artifacts.create_chart", {"chart_spec": {"labels": {"item": ["a"]}}}, {"artifact_name": "x"}))


class TestTextOnlyHost(IntegrationTestCase):
	def test_claude_ai_gets_no_views_even_when_it_declares_apps(self):
		svc = make_service(None)
		caps = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
		with patch("ai_agent.mcp.events.host_name", return_value="Claude-User"):
			svc.set_client_capabilities(caps)
		self.assertIs(svc.ui_supported, False)
		with patch.object(MCPService, "_is_exposed", return_value=True), patch.object(MCPService, "_action_requests_enabled", return_value=False):
			tools = {t["name"]: t for t in svc.list_tools()}
		self.assertFalse([n for n, t in tools.items() if "_meta" in t])
		self.assertNotIn("view__page", tools, "app-only tools need a rendered view")
		self.assertIn("app__choose", tools)

	def test_other_hosts_declaring_apps_keep_views(self):
		svc = make_service(None)
		caps = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
		with patch("ai_agent.mcp.events.host_name", return_value="openai-mcp/1.0.0"):
			svc.set_client_capabilities(caps)
		self.assertIs(svc.ui_supported, True)
