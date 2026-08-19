from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime import local_ocr
from ai_agent.runtime.local_ocr import (
	LocalOCRExecutionError,
	_memory_cap_preexec,
	_wait_for_available_memory,
	_worker_memory_mb,
)


class TestLocalOCRMemoryGuards(IntegrationTestCase):
	def setUp(self):
		self._conf_keys = (
			"ai_agent_ocr_memory_mb",
			"ai_agent_ocr_min_available_mb",
			"ai_agent_ocr_memory_wait_seconds",
		)
		self._previous = {key: frappe.conf.get(key) for key in self._conf_keys}

	def tearDown(self):
		for key in self._conf_keys:
			if self._previous[key] is None:
				frappe.conf.pop(key, None)
			else:
				frappe.conf[key] = self._previous[key]

	def test_passes_when_memory_is_available(self):
		frappe.conf["ai_agent_ocr_min_available_mb"] = 1000
		with patch.object(local_ocr, "_available_memory_mb", return_value=4000):
			_wait_for_available_memory()

	def test_fails_fast_when_memory_is_starved(self):
		frappe.conf["ai_agent_ocr_min_available_mb"] = 1536
		frappe.conf["ai_agent_ocr_memory_wait_seconds"] = 0
		with patch.object(local_ocr, "_available_memory_mb", return_value=300):
			with self.assertRaises(LocalOCRExecutionError) as ctx:
				_wait_for_available_memory()
		self.assertIn("300", str(ctx.exception))
		self.assertIn("1536", str(ctx.exception))

	def test_unreadable_meminfo_never_blocks(self):
		with patch.object(local_ocr, "_available_memory_mb", return_value=0):
			_wait_for_available_memory()

	def test_worker_memory_cap_configuration(self):
		frappe.conf["ai_agent_ocr_memory_mb"] = 3072
		self.assertEqual(_worker_memory_mb(), 3072)
		frappe.conf["ai_agent_ocr_memory_mb"] = 100
		self.assertEqual(_worker_memory_mb(), 512)
		self.assertTrue(callable(_memory_cap_preexec()))
