"""The MCP endpoint over real HTTP (WSGI test client): what a Streamable HTTP client such as Cline, ChatGPT or
Claude does around the JSON-RPC POSTs — the GET for a server stream and the DELETE that ends a session."""

import json
from threading import Thread
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_test_client

ENDPOINT = "/api/method/ai_agent.mcp.api.handle"


class _Request(Thread):
	"""The WSGI client needs its own request context; the test's own is left untouched."""

	def __init__(self, site, sid, method, **kwargs):
		super().__init__()
		self.site, self.sid, self.method, self.kwargs, self.response = site, sid, method, kwargs, None

	def run(self):
		with patch("frappe.app.get_site_name", return_value=self.site):
			client = get_test_client()
			client.set_cookie("sid", self.sid)
			self.response = client.open(ENDPOINT, method=self.method, **self.kwargs)


class TestStreamableHttpEndpoint(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		from frappe.auth import CookieManager, LoginManager
		from frappe.utils import set_request

		set_request(path="/")
		frappe.local.cookie_manager = CookieManager()
		frappe.local.login_manager = LoginManager()
		frappe.local.login_manager.login_as("Administrator")
		cls.sid = frappe.session.sid
		cls.site = frappe.local.site

	def _call(self, method, **kwargs):
		t = _Request(self.site, self.sid, method, **kwargs)
		t.start()
		t.join()
		return t.response

	def test_get_says_no_server_stream_with_405(self):
		"""Client SDKs continue after a 405 on the GET stream; anything else is reported as a failed connection."""
		response = self._call("GET", headers={"Accept": "text/event-stream"})
		self.assertEqual(response.status_code, 405)
		self.assertEqual(response.headers.get("Allow"), "POST")

	def test_delete_session_is_405(self):
		self.assertEqual(self._call("DELETE").status_code, 405)

	def test_post_still_serves_json_rpc(self):
		response = self._call(
			"POST",
			data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
			content_type="application/json",
			headers={"Accept": "application/json, text/event-stream"},
		)
		self.assertEqual(response.status_code, 200)
		body = response.get_json()
		self.assertIn("tools", body["result"])
