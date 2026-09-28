"""Phase B3: question routing and per-route tool sets."""

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime import router
from ai_agent.runtime.context_builder import ContextBuilder
from ai_agent.runtime.orchestrator import Orchestrator
from ai_agent.tests.utils import delete_docs, make_test_run


class TestRouterClassification(IntegrationTestCase):
	CASES = {
		"كم المبيعات اليوم مجمعة على مستوى الفرع وطريقة الدفع؟": "metric",
		"كم لنا عند العملاء، وكم منها متأخر أكثر من 90 يوماً؟": "metric",
		"ما الأصناف التي رصيدها تحت حد إعادة الطلب؟": "metric",
		"أنشئ فاتورة بيع للعميل أحمد": "action",
		"please create a customer": "action",
		"ليش ما تظهر حركة المخزون في الفاتورة ACC-SINV-2026-00012": "diagnose",
		"اعطني تفاصيل ACC-PINV-2026-00003": "document_lookup",
		"أحدث فاتورة": "document_lookup",
		"كيف أضيف صنف جديد؟": "guide",
		"حلل أداء العملاء واقترح خطة": "open_analysis",
		"credit note total": "open_analysis",  # "edit" inside "credit" is not an action
		"كم المبلغ البالغ": "open_analysis",  # "الغ" inside "البالغ" is not a cancel
	}

	def test_classification(self):
		for text, expected in self.CASES.items():
			self.assertEqual(router.classify(text), expected, text)

	def test_attachments_route_to_invoice(self):
		self.assertEqual(router.classify("اقرأ هذه الفاتورة", has_attachments=True), "invoice")

	def test_route_tool_sets_have_at_most_eight_tools(self):
		for route in router.ROUTES:
			tools = router.route_tools(route)
			if route == "open_analysis":
				self.assertIsNone(tools)
				continue
			self.assertLessEqual(len(tools), 8, route)
			self.assertIn(router.REQUEST_MORE, tools, route)

	def test_only_open_analysis_gets_exploration_tools(self):
		for route in router.ROUTES:
			tools = router.route_tools(route) or []
			self.assertNotIn("schema.search_doctypes", tools, route)
			self.assertNotIn("schema.describe_links", tools, route)


class TestRoutedRun(IntegrationTestCase):
	def setUp(self):
		self.run = make_test_run(prompt="كم المبيعات هذا الشهر؟")
		self.orch = Orchestrator(self.run.name)
		self.orch._configure_skill_runtime(
			self.orch.skill_registry.list_effective_skills(self.run.user, self.run.agent, None)
		)
		self.orch._configure_route()

	def tearDown(self):
		delete_docs("AI Run", [self.run.name])
		frappe.db.commit()

	def test_metric_route_tools_and_prompt(self):
		self.assertEqual(self.orch.route, "metric")
		names = {schema["name"].replace("__", ".") for schema in self.orch._get_tool_schemas()}
		self.assertLessEqual(names, {"metrics.query", router.REQUEST_MORE})
		self.assertIn(router.REQUEST_MORE, names)
		self.assertEqual(frappe.db.get_value("AI Run", self.run.name, "route"), "metric")

		prompt = ContextBuilder()._build_system_prompt(
			self.orch.agent, [], self.orch._get_tool_schemas(), route="metric"
		)
		self.assertNotIn("declarative components", prompt)  # interactive UI block dropped
		self.assertIn("Per-run context:", prompt)
		self.assertLess(prompt.index("metrics.query"), prompt.index("Per-run context:"))

	def test_request_more_only_grants_skill_declared_tools(self):
		result = self.orch._handle_request_more({"tools": ["schema.describe_doctype", "no.such_tool"]})
		self.assertEqual(result["added"], ["schema.describe_doctype"])
		self.assertEqual(result["unavailable"], ["no.such_tool"])
		names = {schema["name"].replace("__", ".") for schema in self.orch._get_tool_schemas()}
		self.assertIn("schema.describe_doctype", names)
