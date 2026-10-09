from frappe.tests import IntegrationTestCase

from ai_agent.runtime.local_answers import greeting_or_capability_answer


class TestLocalAnswers(IntegrationTestCase):
	def test_data_question_is_not_hijacked_by_substring(self):
		# Regression: "hi" used to match inside "this"/"which".
		self.assertIsNone(greeting_or_capability_answer("What is this month's total for us?"))
		self.assertIsNone(greeting_or_capability_answer("Which supplier do we owe most?"))

	def test_short_greeting_gets_canned_answer(self):
		self.assertIsNotNone(greeting_or_capability_answer("hi"))
		self.assertIsNotNone(greeting_or_capability_answer("مرحبا"))
		self.assertIsNotNone(greeting_or_capability_answer("السلام عليكم"))

	def test_action_prompt_always_reaches_model(self):
		self.assertIsNone(greeting_or_capability_answer("مرحبا، انشئ فاتورة للعميل"))
		self.assertIsNone(greeting_or_capability_answer("hello, create an invoice"))

	def test_capability_question_gets_canned_answer(self):
		self.assertIsNotNone(greeting_or_capability_answer("هل تستطيع التعامل مع المهام الكبيره؟"))

	def test_long_prompt_mentioning_capability_is_not_hijacked(self):
		self.assertIsNone(
			greeting_or_capability_answer(
				"Can you help me figure out why the stock levels are off between the two warehouses this quarter?"
			)
		)


