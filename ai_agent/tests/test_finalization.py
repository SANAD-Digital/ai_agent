import json

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.answer_contract import looks_like_tool_markup
from ai_agent.runtime.orchestrator import Orchestrator
from ai_agent.tests.utils import delete_docs, make_test_run

COMPOUND_POLICY_SKILL = {
	"name": "fake-compound-skill",
	"allow_implicit_invocation": 1,
	"runtime_policy": {
		"response_format": "JSON Object",
		"required_output_keys": ["invoice_groups", "questions", "warnings", "errors"],
	},
}

DSML_ANSWER = (
	'["name", "account_name"]</｜｜DSML｜｜parameter> '
	'<｜｜DSML｜｜parameter name="filters" string="false">'
	'{"field": "company", "operator": "=", "value": "تجربة"}'
)


class TestFinalization(IntegrationTestCase):
	def setUp(self):
		self.run = make_test_run()
		self.orch = Orchestrator(self.run.name)

	def tearDown(self):
		delete_docs("AI Run", [self.run.name])
		frappe.db.commit()

	def _with_compound_policy(self):
		self.orch.effective_skills = [dict(COMPOUND_POLICY_SKILL)]

	def test_tool_markup_is_never_mined_for_json(self):
		# Regression for AI-RUN-0878: raw DSML tool markup contained a tool
		# ARGUMENT object which was extracted and normalized into the final
		# answer. Markup must route to the fallback contract instead.
		self._with_compound_policy()
		result = json.loads(self.orch._ensure_required_json_final(DSML_ANSWER))
		self.assertNotIn("field", result)
		self.assertIn("invoice_groups", result)

	def test_markup_detection(self):
		self.assertTrue(looks_like_tool_markup(DSML_ANSWER))
		self.assertTrue(looks_like_tool_markup('<invoke name="data.query">'))
		self.assertFalse(looks_like_tool_markup('{"invoice_groups": []}'))

	def test_empty_memory_search_is_not_terminal(self):
		result = {"rows": [], "row_count": 0, "candidate_count": 0, "message": "no memory"}
		self.assertFalse(self.orch._is_terminal_tool_result("memory.search", result))

	def test_positive_signals_not_terminal_under_compound_contract(self):
		# Regression: exhaustive/rows>0 forced finalization mid-task while the
		# skill contract (invoice_groups) was still unmet.
		self._with_compound_policy()
		self.assertFalse(
			self.orch._is_terminal_tool_result("memory.search", {"exhaustive": True})
		)
		self.assertFalse(
			self.orch._is_terminal_tool_result("reports.run", {"row_count": 5})
		)

	def test_positive_signals_still_terminal_without_contract(self):
		self.orch.effective_skills = []
		self.assertTrue(
			self.orch._is_terminal_tool_result("reports.run", {"row_count": 5})
		)

	def test_blocking_signals_terminal_even_under_contract(self):
		self._with_compound_policy()
		self.assertTrue(
			self.orch._is_terminal_tool_result(
				"actions.request_submit", {"approval_required": True, "approval_id": "APR-1"}
			)
		)
		self.assertTrue(
			self.orch._is_terminal_tool_result("data.query", {"error": "No read permission"})
		)

	def test_valid_json_answer_passes_untouched(self):
		self._with_compound_policy()
		answer = '{"invoice_groups": [{"x": 1}], "questions": [], "warnings": [], "errors": []}'
		self.assertEqual(json.loads(self.orch._ensure_required_json_final(answer)), json.loads(answer))
