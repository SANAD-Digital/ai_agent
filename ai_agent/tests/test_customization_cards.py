"""The customization section of the approval card (group C, design §3.4) on both surfaces.

The payloads below follow customization/contract.py exactly; nothing here needs the kinds (group B) or a migrated
"Customize" action type: the approvals are in-memory records, as the card builder only reads them.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization import presenter
from ai_agent.mcp import approvals

SERVER_SCRIPT = """# ai_agent: Block invoices with a discount above 20% | APR-CZ-1 | Administrator | 2026-10-19
limit = 20
if doc.additional_discount_percentage and doc.additional_discount_percentage > limit:
    frappe.throw(f"Discount {doc.additional_discount_percentage}% is above the {limit}% limit")

customer = frappe.db.get_value("Customer", doc.customer, "customer_group")
if customer == "Wholesale" and doc.grand_total < 1000:
    frappe.msgprint("Wholesale invoice below 1,000")
""" + "\n".join(f"# note {i}" for i in range(1, 260))  # long on purpose: the card must never shorten code

CLIENT_BEFORE = """frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        frm.set_df_property("po_no", "reqd", 1);
    }
});"""
CLIENT_AFTER = """frappe.ui.form.on("Sales Invoice", {
    refresh(frm) {
        frm.set_df_property("po_no", "reqd", 1);
        eval(frm.doc.remarks);
    }
});"""
QUERY = """select employee, employee_name, date(time) as day, count(*) as punches
from `tabEmployee Checkin`
where time between %(from_date)s and %(to_date)s
group by employee, date(time)
order by day desc"""

MSG = {
	"ar": {
		"syntax": "الصياغة سليمة", "perm": "يقرأ Customer بلا فحص صلاحية (frappe.db.get_value)", "info": "سكربت آخر مفعّل على الحدث نفسه: Sales Invoice Discount",
		"impact1": "يعمل عند حفظ كل فاتورة مبيعات (~420 شهرياً)", "impact2": "لكل المستخدمين، وفي الاستيراد وواجهة REST",
		"eval": "eval ممنوع: ينفّذ نصاً كأنه كود", "cs_impact": "يعمل في متصفح كل من يفتح نموذج فاتورة المبيعات (37 مستخدماً)",
		"pr_ok": "قبل Frappe كل الخصائص", "pr_warn": "المستندات القديمة ستطلب هذا الحقل عند أول حفظ (1,284 فاتورة)",
		"cf_ok": "الاسم غير مكرر وغير محجوز", "cf_info": "الحقل سيظهر بعد «العميل»",
		"rp_warn": "الدور Employee يرى عبر التقرير بصمات لا يراها في القائمة", "rp_ok": "كل متغير له فلتر",
		"purpose": "منع الخصم فوق 20% في فواتير المبيعات", "v_ok": "السكربت محفوظ بالكود نفسه ومفعّل", "v_map": "مسجّل على حدث «قبل الحفظ»",
	},
	"en": {
		"syntax": "Syntax is valid", "perm": "Reads Customer without a permission check (frappe.db.get_value)", "info": "Another enabled script runs on the same event: Sales Invoice Discount",
		"impact1": "Runs on every Sales Invoice save (~420 a month)", "impact2": "For every user, including imports and the REST API",
		"eval": "eval is not allowed: it runs text as code", "cs_impact": "Runs in the browser of everyone who opens a Sales Invoice (37 users)",
		"pr_ok": "Frappe accepted every property", "pr_warn": "Existing documents will ask for this field on their next save (1,284 invoices)",
		"cf_ok": "The name is unique and not reserved", "cf_info": "The field appears after Customer",
		"rp_warn": "The Employee role sees check-ins through the report that it cannot see in the list", "rp_ok": "Every parameter has a filter",
		"purpose": "Block discounts above 20% on sales invoices", "v_ok": "The script is saved with this exact code and enabled", "v_map": "Registered on the Before Save event",
	},
}


def _payload(name, m):
	ctx = {"surface": "Desk", "tainted": False, "via": "customize.request_server_script", "requested_by": "Administrator"}
	if name in ("server_script", "executed"):
		return {
			"kind": "server_script", "operation": "create",
			"spec": {"operation": "create", "name": "Sales Invoice - Discount Limit", "script_type": "DocType Event", "reference_doctype": "Sales Invoice",
				"doctype_event": "Before Save", "script": SERVER_SCRIPT, "purpose": m["purpose"]},
			"before": {}, "summary": m["purpose"], "purpose": m["purpose"], "target": "server_script:Sales Invoice - Discount Limit",
			"report": {"risk": "high", "impact": [m["impact1"], m["impact2"]],
				"checks": [{"code": "py.syntax", "level": "ok", "message": m["syntax"]}, {"code": "py.unpermitted_read", "level": "warning", "message": m["perm"], "line": 6},
					{"code": "py.same_event", "level": "info", "message": m["info"]}],
				"preview": {"dry_run": {"document": "ACC-SINV-2026-00042", "threw": False, "changed": {}}}},
			"context": {**ctx, "tainted": name == "server_script"},
		}
	if name == "client_script_update":
		return {
			"kind": "client_script", "operation": "update",
			"spec": {"operation": "update", "name": "Sales Invoice - PO Required", "doctype": "Sales Invoice", "view": "Form", "script": CLIENT_AFTER},
			"before": {"name": "Sales Invoice - PO Required", "script": CLIENT_BEFORE, "enabled": 1},
			"report": {"risk": "medium", "impact": [m["cs_impact"]], "checks": [{"code": "js.eval", "level": "critical", "message": m["eval"], "line": 4}], "preview": {}},
			"context": {**ctx, "via": "customize.request_client_script"},
		}
	if name == "properties":
		return {
			"kind": "properties", "operation": "update",
			"spec": {"doctype": "Sales Invoice", "field_properties": {"po_no": {"reqd": 1, "in_list_view": 1}, "remarks": {"label": "ملاحظات العميل" if m is MSG["ar"] else "Customer notes"}},
				"doctype_properties": {"quick_entry": 0}},
			"before": {"doctype_properties": {"quick_entry": 1}, "fields": {"po_no": {"reqd": 0, "in_list_view": 0}, "remarks": {"label": "Remarks"}}},
			"report": {"risk": "low", "impact": [m["pr_warn"]], "checks": [{"code": "props.allowed", "level": "ok", "message": m["pr_ok"]}, {"code": "field.reqd_existing", "level": "warning", "message": m["pr_warn"]}],
				"preview": {"list_columns": ["Customer", "Status", "Grand Total", "Customer's Purchase Order"], "standard_filters": ["Customer", "Status"]}},
			"context": {**ctx, "via": "customize.request_properties"},
		}
	if name == "custom_field":
		return {
			"kind": "custom_field", "operation": "add",
			"spec": {"doctype": "Sales Invoice", "operation": "add", "fields": [{"fieldname": "custom_delivery_zone", "label": "منطقة التوصيل" if m is MSG["ar"] else "Delivery zone",
				"fieldtype": "Select", "options": "North\nSouth\nEast", "insert_after": "customer", "in_standard_filter": 1}]},
			"before": {},
			"report": {"risk": "low", "impact": [], "checks": [{"code": "field.name", "level": "ok", "message": m["cf_ok"]}, {"code": "field.place", "level": "info", "message": m["cf_info"]}],
				"preview": {"layout": {"section": "Customer", "fields": [{"label": "Customer", "fieldname": "customer", "fieldtype": "Link"},
					{"label": "Delivery zone", "fieldname": "custom_delivery_zone", "fieldtype": "Select", "new": True}, {"label": "Customer Name", "fieldname": "customer_name", "fieldtype": "Data"}]}}},
			"context": {**ctx, "via": "customize.request_field"},
		}
	if name == "report":
		return {
			"kind": "report", "operation": "create",
			"spec": {"operation": "create", "report_name": "Daily Check-ins", "ref_doctype": "Employee Checkin", "report_type": "Query Report", "query": QUERY,
				"filters": [{"fieldname": "from_date", "label": "From", "fieldtype": "Date"}, {"fieldname": "to_date", "label": "To", "fieldtype": "Date"}]},
			"before": {},
			"report": {"risk": "high", "impact": [m["rp_warn"]], "checks": [{"code": "sql.params", "level": "ok", "message": m["rp_ok"]}, {"code": "sql.role_widening", "level": "warning", "message": m["rp_warn"], "line": 2}],
				"preview": {"filters": [{"label": "From", "fieldtype": "Date"}, {"label": "To", "fieldtype": "Date"}],
					"rows": {"columns": [{"fieldname": "employee", "label": "Employee"}, {"fieldname": "employee_name", "label": "Name"}, {"fieldname": "day", "label": "Day"}, {"fieldname": "punches", "label": "Punches"}],
						"rows": [["HR-EMP-00012", "أحمد علي", "2026-10-18", 4], ["HR-EMP-00031", "سارة محمد", "2026-10-18", 2]]}}},
			"context": {**ctx, "via": "customize.request_report"},
		}
	raise KeyError(name)


CASES = ("server_script", "client_script_update", "properties", "custom_field", "report", "executed")


def approval_for(name, lang="en", status=None, execution_result=None):
	m = MSG[lang]
	payload = _payload(name, m)
	status = status or ("Executed" if name == "executed" else "Pending")
	if name == "executed" and execution_result is None:
		execution_result = {"kind": "server_script", "operation": "create", "targets": [{"doctype": "Server Script", "name": "Sales Invoice - Discount Limit"}],
			"undo": {"operation": "disable", "name": "Sales Invoice - Discount Limit"}, "verified": True,
			"checks": [{"code": "verify.saved", "level": "ok", "message": m["v_ok"]}, {"code": "verify.map", "level": "ok", "message": m["v_map"]}]}
	return frappe._dict(name=f"APR-CZ-{CASES.index(name) + 1}", action_type="Customize", doctype_name=None, document_name=None, status=status,
		payload_json=json.dumps(payload, ensure_ascii=False), payload_hash="h", action_summary=payload.get("summary") or payload["kind"], impact_summary="",
		expires_at=None, run=None, requested_by="Administrator", execution_result=json.dumps(execution_result) if execution_result else None)


class EnglishCase(IntegrationTestCase):
	def setUp(self):
		frappe.local.ai_label_lang = None
		lang = patch("ai_agent.presentation.labels.label_language", return_value="en")
		lang.start()
		self.addCleanup(lang.stop)


class TestCustomizationSection(EnglishCase):
	def test_not_a_customization(self):
		self.assertIsNone(presenter.section(frappe._dict(action_type="Create", payload_json="{}")))

	def test_full_code_is_never_shortened(self):
		section = presenter.section(approval_for("server_script"), "Administrator")
		self.assertEqual(section["code"]["text"], SERVER_SCRIPT)
		self.assertEqual(section["code"]["lines"], SERVER_SCRIPT.count("\n") + 1)
		self.assertGreater(section["code"]["lines"], 250)
		self.assertEqual(section["code"]["language"], "python")

	def test_checks_sorted_with_lines_and_counts(self):
		section = presenter.section(approval_for("server_script"), "Administrator")
		self.assertEqual([c["level"] for c in section["checks"]], ["warning", "info", "ok"])
		self.assertEqual(section["checks"][0]["line"], 6)
		self.assertEqual(section["counts"]["warning"], 1)
		self.assertEqual(section["risk"], "high")
		self.assertEqual(section["impact"][0], MSG["en"]["impact1"])
		self.assertIn("high-risk", section["approve_label"].lower())

	def test_critical_hides_approve(self):
		section = presenter.section(approval_for("client_script_update"), "Administrator")
		self.assertTrue(section["has_critical"])
		self.assertFalse(section["can_approve"])
		self.assertTrue(section["approve_hidden_reason"])

	def test_never_always_approve(self):
		for name in CASES:
			self.assertIs(presenter.section(approval_for(name), "Administrator")["allow_always"], False, name)

	def test_update_shows_unified_diff(self):
		code = presenter.section(approval_for("client_script_update"), "Administrator")["code"]
		self.assertEqual(code["text"], CLIENT_AFTER)
		self.assertIn("+        eval(frm.doc.remarks);", code["diff"])
		self.assertEqual(code["language"], "javascript")

	def test_tainted_banner(self):
		self.assertTrue(presenter.section(approval_for("server_script"), "Administrator")["tainted_text"])
		self.assertIsNone(presenter.section(approval_for("properties"), "Administrator")["tainted_text"])

	def test_properties_diff_and_lists(self):
		section = presenter.section(approval_for("properties"), "Administrator")
		rows = {(r["field"], r["property"]): r for r in section["diff"]}
		self.assertEqual((rows[("po_no", "reqd")]["before"], rows[("po_no", "reqd")]["after"]), ("No", "Yes"))
		self.assertEqual(rows[("remarks", "label")]["after"], "Customer notes")
		self.assertIn(("", "quick_entry"), rows)
		self.assertEqual([l["key"] for l in section["lists"]], ["list_columns", "standard_filters"])
		self.assertIsNone(section["code"])

	def test_custom_field_blocks_and_layout(self):
		section = presenter.section(approval_for("custom_field"), "Administrator")
		self.assertEqual(section["fields"][0]["fieldname"], "custom_delivery_zone")
		self.assertTrue(any(f["new"] for f in section["layout"]["fields"]))
		self.assertIn("Remove", section["undo"])

	def test_query_report_code_and_rows(self):
		section = presenter.section(approval_for("report"), "Administrator")
		self.assertEqual(section["code"]["language"], "sql")
		self.assertEqual(section["code"]["text"], QUERY)
		self.assertEqual(section["rows"]["columns"], ["Employee", "Name", "Day", "Punches"])
		self.assertEqual(section["rows"]["rows"][0][3], "4")

	def test_executed_offers_open_and_undo(self):
		section = presenter.section(approval_for("executed"), "Administrator")
		self.assertEqual(section["state"], "verified")
		self.assertFalse(section["can_approve"])
		self.assertEqual(section["result"]["targets"][0]["doctype"], "Server Script")
		self.assertIn("/app/server-script/", section["result"]["targets"][0]["href"])
		self.assertIn("APR-CZ-6", section["result"]["undo_prompt"])
		self.assertEqual(len(section["result"]["checks"]), 2)

	def test_four_eyes_hides_own_approve(self):
		with patch("ai_agent.customization.policy.four_eyes", return_value=True):
			section = presenter.section(approval_for("properties"), "Administrator")
		self.assertFalse(section["can_approve"])
		self.assertTrue(section["approve_hidden_reason"])

	def test_approver_without_roles(self):
		with patch("ai_agent.customization.policy.has_roles", return_value=False):
			section = presenter.section(approval_for("properties"), "someone@example.com")
		self.assertFalse(section["can_approve"])


class TestCustomizationMcpCard(EnglishCase):
	def setUp(self):
		super().setUp()
		frappe.flags.mcp_ui_supported = True
		self.addCleanup(lambda: frappe.flags.pop("mcp_ui_supported", None))

	def card(self, name, **kw):
		with patch.object(approvals, "app_approval_enabled", return_value=True), patch("ai_agent.mcp.continuation.peek", return_value=None):
			return approvals.build_card(approval_for(name, **kw), "Administrator")

	def test_desk_only_no_approve_button(self):
		for name in ("server_script", "properties", "custom_field", "report"):
			card = self.card(name)
			keys = [a["key"] for a in card["actions"]]
			self.assertNotIn("approve", keys, name)
			self.assertEqual(keys[0], "open_request")
			self.assertIn("/app/ai-approval-request/", card["actions"][0]["href"])
			self.assertIn("desk_only", [w["code"] for w in card["warnings"]])
			self.assertEqual(card["customization"]["kind"], json.loads(approval_for(name).payload_json)["kind"])

	def test_tainted_warning_on_card(self):
		self.assertIn("tainted", [w["code"] for w in self.card("server_script")["warnings"]])

	def test_executed_card_open_and_undo(self):
		card = self.card("executed")
		self.assertEqual(card["state"], "verified")
		keys = [a["key"] for a in card["actions"]]
		self.assertEqual(keys, ["open", "undo"])
		undo = card["actions"][1]
		self.assertEqual(undo["kind"], "message")
		self.assertIn("APR-CZ-6", undo["arguments"]["text"])

	def test_text_fallback_names_risk_and_desk(self):
		with patch.object(approvals, "app_approval_enabled", return_value=True), patch("ai_agent.mcp.continuation.peek", return_value=None):
			envelope = approvals.card_envelope(approval_for("server_script"), "Administrator")
		self.assertIn("/app/ai-approval-request/", envelope["summary"]["text"])
		from ai_agent.presentation import text as presentation_text

		text = presentation_text.render(envelope["presentation"], envelope["summary"]["text"])
		self.assertIn(MSG["en"]["perm"], text)

	def test_model_cannot_approve_over_mcp(self):
		apr = approval_for("properties")
		with patch.object(approvals, "_load", return_value=apr), patch.object(approvals, "approval_required", return_value=True):
			with self.assertRaises(approvals.ApprovalUiError):
				approvals.decide("Administrator", apr.name, "approve")

	def test_verify_reads_recorded_verdict(self):
		self.assertTrue(approvals._verify({"targets": [], "verified": True}, "Customize"))
		self.assertFalse(approvals._verify({"targets": [{"doctype": "Server Script", "name": "x"}], "verified": False}, "Customize"))
		self.assertFalse(approvals._verify({"document_name": "x", "doctype": "ToDo"}, "Customize"))
