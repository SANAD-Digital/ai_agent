"""customize.get / customize.inspect reach every kind: the kind's own reader, the new inspect parts, listing by kind
without a DocType, and Server Script code only for Script Manager."""

from unittest.mock import patch

import frappe

from ai_agent.customization import registry
from ai_agent.customization import tools as ctools
from ai_agent.tests import customization_fakes as fx
from ai_agent.tests.test_customization_policy import CustomizationTestCase, _ctx


class TestCustomizeRead(CustomizationTestCase):
	def test_get_uses_the_kinds_own_reader(self):
		class Fake:
			name = "notification"

			def get_source(self, name, user=None):
				return {"kind": "notification", "name": name, "recipients": ["x@example.com"]}

		with patch.object(registry, "get", return_value=Fake()):
			out = ctools.handle_get({"kind": "notification", "name": "N1"}, _ctx("Administrator"))
		self.assertEqual(out["recipients"], ["x@example.com"])   # not the plain Notification field list

	def test_get_passes_the_dashboard_object(self):
		seen = {}

		class Fake:
			def get_source(self, name, user=None, obj=None):
				seen["obj"] = obj
				return {"kind": "dashboard", "name": name}

		with patch.object(registry, "get", return_value=Fake()):
			ctools.handle_get({"kind": "dashboard", "name": "Sales", "object": "chart"}, _ctx("Administrator"))
		self.assertEqual(seen["obj"], "chart")

	def test_inspect_new_parts(self):
		out = ctools.handle_inspect({"doctype": "ToDo", "include": ["permissions", "list_settings", "naming_rules", "dashboards"]}, _ctx("Administrator"))
		self.assertIn("rows", out["permissions"])
		self.assertIn("naming", out)
		self.assertIn("number_cards", out)
		self.assertIn("list_settings", out)

	def test_list_by_kind_without_doctype(self):
		out = ctools.handle_inspect({"kind": "server_script"}, _ctx("Administrator"))
		self.assertEqual(out["doctype"], "Server Script")
		self.assertIn("records", out)
		self.assertIn("error", ctools.handle_inspect({"kind": "nope"}, _ctx("Administrator")))

	def test_server_script_code_needs_script_manager(self):
		out = ctools.handle_get({"kind": "server_script", "name": "anything"}, _ctx(fx.SM_USER))
		self.assertIn("Script Manager", out["error"])


class TestFourEyes(CustomizationTestCase):
	"""Role permissions and Permission Query scripts never let the requester approve their own request."""

	def _approval(self, kind, spec, requester):
		import json

		return frappe._dict(action_type="Customize", requested_by=requester, payload_json=json.dumps({"kind": kind, "spec": spec}))

	def test_requester_cannot_approve_permissions(self):
		from ai_agent.customization import policy as cpolicy

		self.assertTrue(cpolicy.four_eyes("permissions", {}))
		with self.assertRaises(Exception):
			cpolicy.assert_can_approve(self._approval("permissions", {"doctype": "ToDo"}, "Administrator"), "Administrator")

	def test_permission_query_script_needs_a_second_approver(self):
		from ai_agent.customization import policy as cpolicy

		registry.unregister("server_script")   # the real kind, not the test fake
		self.assertTrue(cpolicy.four_eyes("server_script", {"script_type": "Permission Query", "reference_doctype": "ToDo"}))
		self.assertFalse(cpolicy.four_eyes("server_script", {"script_type": "API"}) and not frappe.db.get_single_value("AI Settings", "customization_require_four_eyes"))
