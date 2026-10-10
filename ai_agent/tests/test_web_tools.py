from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.tools import web

PUBLIC = [(2, 1, 6, "", ("93.184.216.34", 443))]


def fake_response(status=200, body=b"", headers=None):
	r = MagicMock()
	r.status_code = status
	r.headers = headers or {}
	r.encoding = "utf-8"
	r.is_redirect = status in (301, 302, 303, 307, 308)
	r.is_permanent_redirect = status in (301, 308)
	r.iter_content = lambda size: [body]
	return r


class TestWebTools(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.settings = frappe.get_single("AI Settings")
		self.old = self.settings.enable_web_browsing
		frappe.db.set_single_value("AI Settings", "enable_web_browsing", 1)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def tearDown(self):
		frappe.db.set_single_value("AI Settings", "enable_web_browsing", self.old or 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")

	def test_internal_addresses_are_refused(self):
		for url in ("http://127.0.0.1:8000/api/method/version", "http://localhost/", "http://169.254.169.254/latest/meta-data",
				"http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/x"):
			result = web.handle_fetch({"url": url}, None)
			self.assertIn("error", result, url)

	def test_redirect_to_internal_address_is_refused(self):
		redirect = fake_response(302, headers={"Location": "http://127.0.0.1:8000/secret"})
		with patch("socket.getaddrinfo", side_effect=lambda host, *a, **k: PUBLIC if host == "example.com" else [(2, 1, 6, "", ("127.0.0.1", 80))]), \
				patch("requests.Session.request", return_value=redirect):
			result = web.handle_fetch({"url": "https://example.com/go"}, None)
		self.assertIn("internal network", result["error"])

	def test_reads_html_text_title_and_links(self):
		html = b"<html><head><title>Pricing</title><script>bad()</script></head><body><nav>menu</nav><main><h1>Free tier</h1><p>Gemini Flash is free.</p><a href='/docs'>Docs</a></main></body></html>"
		with patch("socket.getaddrinfo", return_value=PUBLIC), \
				patch("requests.Session.request", return_value=fake_response(200, html, {"Content-Type": "text/html"})):
			result = web.handle_fetch({"url": "example.com/pricing"}, None)
		self.assertEqual(result["title"], "Pricing")
		self.assertIn("Gemini Flash is free.", result["content"])
		self.assertNotIn("bad()", result["content"])
		self.assertNotIn("menu", result["content"])
		self.assertEqual(result["links"][0]["url"], "https://example.com/docs")

	def test_turned_off_in_settings(self):
		frappe.db.set_single_value("AI Settings", "enable_web_browsing", 0)
		frappe.clear_document_cache("AI Settings", "AI Settings")
		self.assertIn("error", web.handle_search({"query": "x"}, None))
		self.assertIn("error", web.handle_fetch({"url": "https://example.com"}, None))

	def test_duckduckgo_results_are_unwrapped(self):
		page = b'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fai.google.dev%2Fpricing&rut=x">Gemini pricing</a><a class="result__snippet">Free tier</a></div>'
		with patch("socket.getaddrinfo", return_value=PUBLIC), \
				patch("requests.Session.request", return_value=fake_response(200, page, {"Content-Type": "text/html"})):
			result = web.handle_search({"query": "gemini free tier"}, None)
		self.assertEqual(result["results"][0], {"title": "Gemini pricing", "url": "https://ai.google.dev/pricing", "snippet": "Free tier"})

	def test_engine_offers_web_group_only_when_enabled(self):
		from ai_agent.engine.tools import ToolExecutor
		from ai_agent.runtime.permission_context import PermissionContext

		agent = frappe._dict(name="ERP Analyst", read_only=1, allow_actions=0)
		on = ToolExecutor(PermissionContext("Administrator"), agent, run_name=None, settings=frappe._dict(enable_web_browsing=1, enable_memory=1))
		off = ToolExecutor(PermissionContext("Administrator"), agent, run_name=None, settings=frappe._dict(enable_web_browsing=0, enable_memory=1))
		self.assertIn("web", [g["name"] for g in on.groups_for_prompt()])
		self.assertNotIn("web", [g["name"] for g in off.groups_for_prompt()])
		self.assertFalse(off.allowed("web.fetch"))

	def test_not_exposed_over_mcp(self):
		from ai_agent.mcp.service import MCPService

		svc = MCPService.__new__(MCPService)
		svc.agent = frappe._dict(read_only=1, allow_actions=0)
		svc.settings = frappe._dict()
		self.assertFalse(svc._is_exposed({"name": "web.fetch", "side_effect": "read_only"}))
