"""An AI connector's OAuth token opens the MCP endpoint only (ai_agent/mcp/token_scope.py)."""

from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.mcp import token_scope
from ai_agent.mcp.api import MCP_ENDPOINT


class TestMCPTokenScope(IntegrationTestCase):
	def setUp(self):
		token_scope.clear_cache()
		self.addCleanup(token_scope.clear_cache)
		self.addCleanup(frappe.set_user, "Administrator")

	def _call(self, path, client):
		frappe.set_user("Administrator")
		with (
			patch.object(frappe, "get_request_header", return_value="Bearer tok123"),
			patch.object(frappe, "request", SimpleNamespace(path=path), create=True),
			patch.object(token_scope.frappe.db, "get_value", side_effect=lambda dt, *a, **k: client if dt == "OAuth Bearer Token" else _client_row(client, *a, **k)),
		):
			token_scope.restrict_mcp_tokens()

	def test_mcp_client_is_confined_to_the_endpoint(self):
		self._call(MCP_ENDPOINT, "dyn")   # allowed
		self.assertEqual(frappe.session.user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			self._call("/api/resource/Sales Invoice", "dyn")
		self.assertEqual(frappe.session.user, "Guest")

	def test_oauth_endpoints_stay_open(self):
		self._call("/api/method/frappe.integrations.oauth2.openid_profile", "dyn")
		self.assertEqual(frappe.session.user, "Administrator")

	def test_a_client_registered_by_hand_keeps_the_full_api(self):
		self._call("/api/resource/Sales Invoice", "manual")
		self.assertEqual(frappe.session.user, "Administrator")

	def test_known_ai_callback_counts_as_mcp(self):
		with self.assertRaises(frappe.PermissionError):
			self._call("/api/method/frappe.client.get_list", "claude")


def _client_row(client, *args, **kwargs):
	rows = {
		"dyn": frappe._dict(owner="Guest", redirect_uris="https://example.com/cb", default_redirect_uri=None),
		"manual": frappe._dict(owner="Administrator", redirect_uris="https://erp-mobile.example.com/cb", default_redirect_uri=None),
		"claude": frappe._dict(owner="Administrator", redirect_uris="https://claude.ai/api/mcp/auth_callback", default_redirect_uri=None),
	}
	return rows.get(client)
