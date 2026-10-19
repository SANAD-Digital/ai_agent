"""Every tagged example in skills/customization/references passes the same checks the tools run.

A fenced block is tested when its info string carries a tag after the language:

- ```python server-script type=event|api|permission_query doctype="…" event="…" method=… args="k=v k=@DocType"```
  → ``analyzers.python_code.analyze`` (no critical; not for permission_query, which the tools never offer) and a
  real ``safe_exec`` run on a document of that DocType (events) or with ``frappe.form_dict`` (APIs), inside a
  savepoint that is rolled back. ``frappe.throw`` is an accepted outcome; any other exception fails.
- ```python script-report``` → ``safe_exec`` with the Script Report locals (``filters``, ``data``, ``result``).
- ```js client-script doctype="…" view=Form|List``` → ``analyzers.js_code.analyze`` (node + acorn), no critical
  except a call to an API method that an example server script defines.
- ```sql query-report ref="…" filters="name:Fieldtype[/Options] …"``` → ``analyzers.sql.analyze`` and a read-only
  preview run.
- ```json customize kind=custom_field|properties|report``` → ``KIND.normalize`` + ``KIND.check`` (never execute).
  ``"query": "<the SELECT above>"`` takes the previous query-report block of the same file.
- ```jinja print-format doctype="…"``` → ``frappe.render_template``; ```python notification-condition doctype="…"```
  → ``frappe.safe_eval`` with the Notification context.

Nothing is committed: ``frappe.db.commit`` is patched and every run happens inside a rolled-back savepoint.
"""

import json
import os
import re
import shlex
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ai_agent.runtime.skill_registry import SKILLS_DIR

REF_DIR = os.path.join(SKILLS_DIR, "customization", "references")
FENCE_RE = re.compile(r"^```([a-z]+) ([a-z-]+)([^\n]*)\n(.*?)\n```", re.M | re.S)
SAVEPOINT = "ai_skill_example"
QUERY_PLACEHOLDER = "<the SELECT above>"


def examples():
	"""[(file, lang, tag, attrs, code, previous_sql)] in file order."""
	out = []
	for name in sorted(os.listdir(REF_DIR)):
		if not name.endswith(".md"):
			continue
		with open(os.path.join(REF_DIR, name), encoding="utf-8") as handle:
			text = handle.read()
		last_sql = None
		for match in FENCE_RE.finditer(text):
			lang, tag, rest, code = match.groups()
			attrs = dict(part.split("=", 1) for part in shlex.split(rest) if "=" in part)
			out.append((name, lang, tag, attrs, code, last_sql))
			if tag == "query-report":
				last_sql = (code, attrs)
	return out


def of(tag):
	return [e for e in examples() if e[2] == tag]


def criticals(checks):
	return [f"{c.code}: {c.message}" for c in checks if c.level == "critical"]


def report_filters(spec):
	"""'company:Link/Company from_date:Date' -> report filter rows (all mandatory)."""
	rows = []
	for item in spec.split():
		fieldname, _sep, kind = item.partition(":")
		fieldtype, _sep, options = kind.partition("/")
		row = {"fieldname": fieldname, "label": fieldname, "fieldtype": fieldtype or "Data", "mandatory": 1}
		if options:
			row["options"] = options
		rows.append(row)
	return rows


def any_name(doctype):
	names = frappe.get_list(doctype, pluck="name", limit=1, order_by="creation desc")
	return names[0] if names else None


def sample_doc(doctype):
	"""The latest readable document (as in a save: doc_before_save loaded), else a new one."""
	name = any_name(doctype)
	if not name:
		return frappe.new_doc(doctype)
	doc = frappe.get_doc(doctype, name)
	doc.load_doc_before_save()
	return doc


def api_methods():
	return {e[3].get("method") for e in of("server-script") if e[3].get("type") == "api"}


class TestCustomizationSkillExamples(IntegrationTestCase):
	def setUp(self):
		commit = patch.object(frappe.db, "commit")
		commit.start()
		self.addCleanup(commit.stop)
		frappe.set_user("Administrator")
		self.form_dict = frappe.local.form_dict
		self.addCleanup(setattr, frappe.local, "form_dict", self.form_dict)

	def run_rolled_back(self, fn):
		frappe.db.savepoint(SAVEPOINT)
		message_log = list(frappe.local.message_log or [])
		try:
			return fn()
		finally:
			frappe.db.rollback(save_point=SAVEPOINT)
			frappe.local.message_log = message_log

	def test_references_carry_examples(self):
		tags = {}
		for e in examples():
			tags[e[2]] = tags.get(e[2], 0) + 1
		self.assertGreaterEqual(tags.get("server-script", 0), 8, tags)
		self.assertGreaterEqual(tags.get("client-script", 0), 6, tags)
		self.assertGreaterEqual(tags.get("query-report", 0), 2, tags)
		self.assertGreaterEqual(tags.get("customize", 0), 8, tags)
		known = {"server-script", "client-script", "query-report", "customize", "script-report", "print-format",
			"notification-condition"}
		self.assertEqual(set(tags) - known, set(), "an example tag the test does not run")

	# -- server scripts ---------------------------------------------------------------
	def test_server_script_examples(self):
		from frappe.utils.safe_exec import is_safe_exec_enabled, safe_exec

		from ai_agent.customization.analyzers import python_code

		if not is_safe_exec_enabled():
			self.skipTest("server scripts are disabled on this bench")
		for file, _lang, _tag, attrs, code, _prev in of("server-script"):
			kind = attrs.get("type")
			label = f"{file}: {attrs}"
			with self.subTest(example=label):
				self.assertIn(kind, ("event", "api", "permission_query"), label)
				if kind == "event":
					self.assertTrue(frappe.db.exists("DocType", attrs["doctype"]), label)
					from ai_agent.customization.kinds.server_script import EVENTS

					self.assertIn(attrs["event"], EVENTS, label)
					self.assertEqual(criticals(python_code.analyze(code, "DocType Event", attrs["doctype"])), [], label)
					doc = sample_doc(attrs["doctype"])

					def run(doc=doc, code=code):
						try:
							safe_exec(code, _locals={"doc": doc}, restrict_commit_rollback=True, script_filename="skill_example")
						except (frappe.ValidationError, frappe.PermissionError):
							pass  # frappe.throw is a legitimate outcome of a validation example

					self.run_rolled_back(run)
				elif kind == "api":
					self.assertEqual(criticals(python_code.analyze(code, "API")), [], label)
					form = frappe._dict()
					for pair in shlex.split(attrs.get("args", "")):
						key, _sep, value = pair.partition("=")
						if value.startswith("@"):
							value = any_name(value[1:])
							if not value:
								self.skipTest(f"no {pair} on this site")
						form[key] = value
					frappe.local.form_dict = form
					frappe.local.response = frappe._dict(docs=[])

					def run(code=code):
						exec_globals, _locals = safe_exec(code, script_filename="skill_example")
						return exec_globals

					exec_globals = self.run_rolled_back(run)
					self.assertIn("message", frappe.local.response, f"{label}: an API must set frappe.response['message']")
					self.assertEqual(dict(exec_globals.frappe.flags), {}, f"{label}: frappe.flags would replace the response")
				else:
					local = {"user": "Administrator", "conditions": ""}
					self.run_rolled_back(lambda code=code, local=local: safe_exec(code, None, local, script_filename="skill_example"))
					self.assertIsInstance(local["conditions"], str, label)

	def test_script_report_examples(self):
		from frappe.utils.safe_exec import is_safe_exec_enabled, safe_exec

		from ai_agent.customization.analyzers import python_code

		if not is_safe_exec_enabled():
			self.skipTest("server scripts are disabled on this bench")
		for file, _lang, _tag, _attrs, code, _prev in of("script-report"):
			with self.subTest(example=file):
				self.assertEqual(criticals(python_code.analyze(code, "API")), [])
				company = frappe.defaults.get_user_default("Company") or any_name("Company")
				local = {"filters": frappe._dict(company=company), "data": None, "result": None}
				self.run_rolled_back(lambda code=code, local=local: safe_exec(code, None, local, script_filename="skill_example"))
				self.assertIsInstance(local["data"], list)
				self.assertEqual(len(local["data"]), 2, "data = [columns, rows]")

	# -- client scripts ---------------------------------------------------------------
	def test_client_script_examples(self):
		from ai_agent.customization.analyzers import js_code

		if not js_code.node_path() or not js_code.acorn_path():
			self.skipTest("node or acorn is not available")
		defined = api_methods()
		for file, _lang, _tag, attrs, code, _prev in of("client-script"):
			label = f"{file}: {attrs}"
			with self.subTest(example=label):
				self.assertIn(attrs.get("view"), ("Form", "List"), label)
				checks = js_code.analyze(code, attrs["view"], attrs["doctype"])
				blocking = [c for c in checks if c.level == "critical"
					and not (c.code == "js.call_missing" and any(m in c.message for m in defined))]
				self.assertEqual(criticals(blocking), [], label)

	# -- query reports ----------------------------------------------------------------
	def test_query_report_examples(self):
		from ai_agent.customization.analyzers import sql

		for file, _lang, _tag, attrs, code, _prev in of("query-report"):
			label = f"{file}: {code.splitlines()[1] if len(code.splitlines()) > 1 else code}"
			with self.subTest(example=label):
				filters = report_filters(attrs.get("filters", ""))
				checks, tables = sql.analyze(code, filters, "Administrator")
				self.assertEqual(criticals(checks), [], label)
				self.assertIn(attrs["ref"], tables, label)
				values = sql.sample_values(filters)
				result = self.run_rolled_back(lambda code=code, values=values: sql.preview(code, values, attrs["ref"]))
				self.assertTrue(result["columns"], label)

	# -- fields, properties, reports (normalize + check, never execute) ----------------
	def test_customize_request_examples(self):
		from ai_agent.customization.kinds import custom_field, properties, report

		kinds = {"custom_field": custom_field.KIND, "properties": properties.KIND, "report": report.KIND}
		for file, _lang, _tag, attrs, code, previous_sql in of("customize"):
			label = f"{file}: {code[:80]}"
			with self.subTest(example=label):
				kind = kinds[attrs["kind"]]
				args = json.loads(code)
				if args.get("query") == QUERY_PLACEHOLDER:
					self.assertIsNotNone(previous_sql, f"{label}: no query-report block above it")
					args["query"] = previous_sql[0]
				if attrs["kind"] == "report":
					self.assertFalse(frappe.db.exists("Report", args["report_name"]), f"{label}: pick an unused report name")
				spec = kind.normalize(args, "Administrator")
				report_ = self.run_rolled_back(lambda kind=kind, spec=spec: kind.check(spec, "Administrator"))
				self.assertEqual(criticals(report_.checks), [], label)

	# -- later-phase references ---------------------------------------------------------
	def test_print_format_examples(self):
		for file, _lang, _tag, attrs, code, _prev in of("print-format"):
			with self.subTest(example=file):
				doc = sample_doc(attrs["doctype"])
				html = self.run_rolled_back(lambda code=code, doc=doc: frappe.render_template(code, {"doc": doc}))
				self.assertIn(doc.name or "", html)

	def test_notification_condition_examples(self):
		from frappe.email.doctype.notification.notification import get_context

		for file, _lang, _tag, attrs, code, _prev in of("notification-condition"):
			with self.subTest(example=file):
				doc = sample_doc(attrs["doctype"])
				if doc.is_new():
					self.skipTest(f"no {attrs['doctype']} on this site")
				value = frappe.safe_eval(code.strip(), None, get_context(doc))
				self.assertIn(bool(value), (True, False))
