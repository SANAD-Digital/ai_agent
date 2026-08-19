from frappe.tests import IntegrationTestCase

from ai_agent.purchase_invoice_ai import api, payloads
from ai_agent.purchase_invoice_ai.payloads import (
	analysis_row_count,
	canonicalize_journal_payload,
	extract_json,
	group_matches_fixed_journal_contract,
	merge_page_chunk_payloads,
	normalize_analysis_payload,
	valid_document_number,
)


def _contract_row(line_no=1):
	return {
		"line_no": line_no, "account": "5110 - مشتريات", "account_match_method": "memory",
		"account_confidence": 0, "account_suggestions": [], "party_type": "", "party": "",
		"supplier_action": "", "debit": 100.0, "credit": 0.0, "description": "سطر",
		"custom_cash_supplier": "", "custom_bill_no": "INV-1", "custom_bill_date": "2026-01-01",
		"custom_tax_id": "", "custom_tax_amount": 0.0, "user_remark": "", "cost_center": "",
		"source_file": "a.pdf", "source_page": 1,
		"line_meta": {
			"document_number": "INV-1", "document_date": "2026-01-01", "supplier_name": "مورد",
			"supplier_tax_id": "", "line_amount": 100.0, "tax_amount": 0.0, "source_text": "نص",
		},
	}


def _contract_group():
	return {
		"document_meta": {
			"document_type": "invoice", "document_title": "فاتورة", "supplier_name": "مورد",
			"supplier_tax_id": "", "document_number": "INV-1", "document_date": "2026-01-01",
			"currency": "SAR", "subtotal": 100.0, "tax_amount": 0.0, "grand_total": 100.0,
			"source_file": "a.pdf", "source_pages": [1],
		},
		"journal_entry_lines": [_contract_row()],
	}


class TestPurchasePayloads(IntegrationTestCase):
	def test_legacy_aliases_point_to_payload_functions(self):
		# The sibling service modules call legacy._x on api; the re-exports
		# must stay identical to the payloads functions.
		self.assertIs(api._extract_json, payloads.extract_json)
		self.assertIs(api._merge_page_chunk_payloads, payloads.merge_page_chunk_payloads)
		self.assertIs(api._canonicalize_journal_payload, payloads.canonicalize_journal_payload)
		self.assertIs(api._analysis_groups, payloads.analysis_groups)
		self.assertIs(api._truncate_for_analysis, payloads.truncate_for_analysis)

	def test_extract_json_prefers_group_payload_over_incidental_object(self):
		text = 'note {"a": 1} then {"invoice_groups": [{"journal_entry_lines": [{"debit": 1}]}]}'
		self.assertIn("invoice_groups", extract_json(text) or {})

	def test_extract_json_reads_fenced_block(self):
		self.assertEqual(extract_json('```json\n{"x": 1}\n```'), {"x": 1})
		self.assertIsNone(extract_json("نص عادي"))

	def test_normalize_maps_document_groups_to_invoice_groups(self):
		normalized = normalize_analysis_payload({"document_groups": [{"lines": []}]})
		self.assertEqual(normalized["invoice_groups"], [{"lines": []}])
		self.assertEqual(normalize_analysis_payload(None), {"invoice_groups": []})

	def test_canonicalize_fills_line_defaults_and_group_ids(self):
		payload = {"invoice_groups": [{"lines": [{"debit": "50", "account": "x"}]}]}
		result = canonicalize_journal_payload(payload)
		group = result["invoice_groups"][0]
		self.assertEqual(group["invoice_group_id"], "GROUP-001")
		line = group["journal_entry_lines"][0]
		self.assertEqual(line["line_no"], 1)
		self.assertEqual(line["debit"], 50.0)
		self.assertEqual(line["credit"], 0.0)
		self.assertEqual(line["line_meta"], {})

	def test_analysis_row_count(self):
		payload = {"document_groups": [{"lines": [{}, {}]}, {"lines": [{}]}]}
		self.assertEqual(analysis_row_count(payload), 3)
		self.assertEqual(analysis_row_count({}), 0)

	def test_merge_page_chunk_payloads(self):
		merged = merge_page_chunk_payloads([
			{"client_readable_summary": "أ", "invoice_groups": [{"x": 1}], "errors": ["e1"]},
			{"document_groups": [{"y": 2}], "warnings": ["w1"],
			 "workflow": {"analysis_failed": 1}},
		])
		self.assertEqual(len(merged["invoice_groups"]), 2)
		self.assertEqual(merged["invoice_groups"][0]["invoice_group_id"], "GROUP-001")
		self.assertEqual(merged["invoice_groups"][1]["invoice_group_id"], "GROUP-002")
		self.assertEqual(merged["errors"], ["e1"])
		self.assertEqual(merged["warnings"], ["w1"])
		self.assertEqual(merged["workflow"], {"analysis_failed": 1})
		self.assertEqual(merged["client_readable_summary"], "أ")

	def test_fixed_journal_contract_check(self):
		self.assertTrue(group_matches_fixed_journal_contract(_contract_group()))
		missing_meta = _contract_group()
		del missing_meta["document_meta"]["currency"]
		self.assertFalse(group_matches_fixed_journal_contract(missing_meta))
		forbidden = _contract_group()
		forbidden["journal_entry_lines"][0]["supplier_name"] = "ممنوع"
		self.assertFalse(group_matches_fixed_journal_contract(forbidden))

	def test_valid_document_number(self):
		self.assertEqual(valid_document_number(" INV-123 "), "INV-123")
		self.assertEqual(valid_document_number("ab"), "")
		self.assertEqual(valid_document_number("310000000000003", tax_identifier="310000000000003"), "")
