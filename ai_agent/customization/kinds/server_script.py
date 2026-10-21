"""Server Scripts: DocType Event and API, never for guests (docs/customization/01 §3.3.4), and the v3 types
Scheduler Event and Permission Query (§3.1).

Checked by ``analyzers.python_code`` (compile + RestrictedPython + AST rules, critical on network,
mail, queues, imports, permission bypass, protected DocTypes; warnings on unchecked reads and writes)
and, for ``Before *`` events, a trial run on a copy of a readable document inside a rolled-back
savepoint. Needs Script Manager (``ServerScript.validate`` -> ``frappe.only_for``) and server scripts
enabled in common_site_config (``server_script_enabled``); this module never changes that setting.

Scheduler Event: runs in the background as Administrator with no user (``ServerScript.execute_scheduled_method``
-> ``safe_exec``), on an explicit ``event_frequency`` (a Cron one needs a ``cron_format`` croniter accepts, as
``ScheduledJobType.validate`` requires). Saving it creates a Scheduled Job Type (``sync_scheduled_job_type``).

Permission Query: the script sets ``conditions``, raw SQL that Frappe appends to every list query of
``reference_doctype`` — ``" and ".join(conditions)`` without parentheses in frappe/model/db_query.py
(``build_match_conditions``/``get_permission_query_conditions``, used by ``get_match_cond`` in link searches),
wrapped as ``(cond)`` by the query builder in frappe/database/query.py. The check runs the script for two sample
users, validates each condition with sqlglot (one expression that stays one expression when ANDed with others: no
top-level OR, no escaping its parentheses, no comments or ``;``, no subquery into protected tables) and builds the
real list query for those users with the new condition in place (no write; see ``_with_permission_query``).
Only one Permission Query per DocType is used (``server_script_utils.get_server_script_map``).
"""

from __future__ import annotations

import contextlib
import re
import threading
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import add_days, cint, now_datetime, nowdate

from ai_agent.customization.analyzers import python_code
from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds._script import ScriptKind

SCRIPT_TYPES = ("DocType Event", "API", "Scheduler Event", "Permission Query")
# ServerScript.event_frequency options (frappe/core/doctype/server_script/server_script.json).
FREQUENCIES = ("All", "Hourly", "Daily", "Weekly", "Monthly", "Yearly", "Hourly Long", "Daily Long", "Weekly Long",
	"Monthly Long", "Cron")
PQ_SAMPLE_USERS = 2
PQ_MAX_CHARS = 5000
PQ_PLACEHOLDER = "ai-agent-permission-query-preview"
STRING_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"")
EVENTS = ("Before Insert", "Before Validate", "Before Save", "After Insert", "After Save", "Before Submit",
	"After Submit", "Before Cancel", "After Cancel", "Before Delete", "After Delete",
	"Before Save (Submitted Document)", "After Save (Submitted Document)", "Before Print")
API_METHOD_RE = re.compile(r"^[a-z][a-z0-9_.]{2,80}$")


_PREVIEW_LOCK = threading.Lock()


def _cron_of(frequency, cron_format=None):
	"""The cron expression Frappe schedules a frequency with (frappe/core/doctype/scheduled_job_type/
	scheduled_job_type.py ``get_next_execution`` CRON_MAP; "Cron" uses cron_format)."""
	if frequency == "Cron":
		return cron_format
	base = (frequency or "").replace(" Long", "")
	return {
		"Yearly": "0 0 1 1 *", "Monthly": "0 0 1 * *", "Weekly": "0 0 * * 0", "Daily": "0 0 * * *",
		"Hourly": "0 * * * *", "All": f"*/{(frappe.get_conf().scheduler_interval or 240) // 60} * * * *",
	}.get(base)


def validate_condition(text, label, doctype=None):
	"""Checks for one SQL condition a Permission Query produced for ``label`` (a user id).

	Frappe appends it as `` and <condition>`` (db_query.py) or ``(<condition>)`` (query.py), so it must be one
	expression that stays one expression next to other ANDed conditions: no comments or ``;``, balanced
	parentheses that never close one Frappe opened, no top-level OR/XOR, no ORDER BY/LIMIT/UNION tail, and
	subqueries only into readable, non-system tables.
	"""
	import sqlglot
	from sqlglot import exp

	from ai_agent.customization.analyzers import sql as sql_analyzer
	from ai_agent.customization.analyzers.python_code import protected_doctype
	from ai_agent.runtime import masking

	text = (text or "").strip()
	if len(text) > PQ_MAX_CHARS:
		return [Check("script.pq_size", "critical", _("For {0} the condition is longer than {1} characters").format(label, PQ_MAX_CHARS))]
	bare = STRING_RE.sub("''", text)
	if any(token in bare for token in ("--", "#", "/*", "*/", ";")):
		return [Check("script.pq_comment", "critical", _("For {0} the condition contains a comment or ';', which could cut off the rest of Frappe's query").format(label))]
	depth = 0
	for char in bare:
		depth += 1 if char == "(" else -1 if char == ")" else 0
		if depth < 0:
			return [Check("script.pq_escape", "critical", _("For {0} the condition closes a parenthesis it did not open: it would escape into Frappe's query").format(label))]
	if depth:
		return [Check("script.pq_escape", "critical", _("For {0} the condition leaves a parenthesis open").format(label))]
	if _dangerous_sql(bare):
		return [Check("script.pq_function", "critical", _("For {0} the condition uses a function or INTO that is not allowed").format(label))]
	try:
		parsed = sqlglot.parse(f"SELECT 1 FROM t WHERE {text}", read="mysql")
	except Exception as e:
		return [Check("script.pq_syntax", "critical", _("For {0} the condition is not valid SQL: {1}").format(label, str(e).splitlines()[0][:200]))]
	parsed = [p for p in parsed if p is not None]
	if len(parsed) != 1 or not isinstance(parsed[0], exp.Select) or not parsed[0].args.get("where"):
		return [Check("script.pq_syntax", "critical", _("For {0} the condition is not one SQL expression").format(label))]
	root = parsed[0]
	extra = sorted(k for k, v in root.args.items() if v and k not in ("expressions", "from", "where"))
	if extra:
		return [Check("script.pq_structure", "critical", _("For {0} the condition adds {1} to Frappe's query; only a WHERE expression is allowed").format(label, ", ".join(extra).upper()))]
	condition = root.args["where"].this
	if isinstance(condition, exp.Or | exp.Xor):
		return [Check("script.pq_top_or", "critical",
			_("For {0} the condition has a top-level OR: Frappe adds it with AND and no parentheses, so it would widen access. Wrap it: (a or b)").format(label))]
	checks = []
	salary = masking.salary_doctypes()
	for table in condition.find_all(exp.Table):
		name = table.name
		if table.args.get("db") or table.args.get("catalog"):
			checks.append(Check("script.pq_table", "critical", _("For {0} the condition reads another database: {1}").format(label, table.sql(dialect="mysql"))))
			continue
		if name in sql_analyzer.BLOCKED_TABLES or name.startswith(("__", "tabOAuth")) or not name.startswith("tab"):
			checks.append(Check("script.pq_table", "critical", _("For {0} the condition reads the system table {1}").format(label, name)))
			continue
		sub = name[3:]
		if not frappe.db.exists("DocType", sub):
			checks.append(Check("script.pq_table", "critical", _("For {0} the condition reads {1}, which is not a DocType table").format(label, name)))
		elif protected_doctype(sub) and sub not in ("User Permission",):
			checks.append(Check("script.pq_table", "critical", _("For {0} the condition reads {1}, which controls code or access").format(label, sub)))
		elif sub in salary:
			checks.append(Check("script.pq_table", "critical", _("For {0} the condition reads pay data ({1})").format(label, sub)))
	if checks:
		return checks
	if doctype and f"`tab{doctype}`" not in text:
		checks.append(Check("script.pq_qualify", "info",
			_("Name the columns as `tab{0}`.`field`: an unqualified column is ambiguous when Frappe joins a child table").format(doctype)))
	checks.append(Check("script.pq_condition", "ok", _("For {0} the condition is one expression that can only narrow the list").format(label)))
	return checks


def _dangerous_sql(bare):
	from ai_agent.customization.analyzers import sql as sql_analyzer

	return bool(sql_analyzer.DANGEROUS_FN_RE.search(bare) or sql_analyzer.INTO_FILE_RE.search(bare))


def server_scripts_enabled():
	from frappe.utils.safe_exec import is_safe_exec_enabled

	return is_safe_exec_enabled()


class ServerScriptKind(ScriptKind):
	name = "server_script"
	setting = "allow_customize_server_scripts"
	required_roles = ("System Manager", "Script Manager")
	doctype = "Server Script"
	tool = "customize.request_server_script"
	comment = "#"
	allowed_keys = ("operation", "name", "script_type", "reference_doctype", "doctype_event", "api_method", "script",
		"enable_rate_limit", "rate_limit_count", "rate_limit_seconds", "purpose", "allow_guest", "event_frequency",
		"cron_format", "sample_users")
	record_fields = ("script_type", "reference_doctype", "doctype_event", "api_method", "enable_rate_limit",
		"rate_limit_count", "rate_limit_seconds", "allow_guest", "event_frequency", "cron_format")
	# Left out of the snapshot while empty, so DocType Event / API snapshots keep their earlier shape.
	optional_snapshot_fields = ("event_frequency", "cron_format")
	enabled_field = "disabled"

	def four_eyes_for(self, spec):
		"""A Permission Query decides who sees which rows (design §3.1 v3: independent review): its
		approver is never its requester — on create, change, enable or disable alike."""
		script_type = (spec or {}).get("script_type")
		if not script_type and (spec or {}).get("name"):
			script_type = frappe.db.get_value(self.doctype, spec["name"], "script_type")
		return script_type == "Permission Query"

	def normalize(self, args, user):
		args = dict(args or {})
		forced = []
		if "allow_guest" in args:
			if cint(args.get("allow_guest")):
				forced.append("allow_guest")
			args.pop("allow_guest")
		spec = super().normalize(args, user)
		if spec["operation"] in ("create", "update"):
			for key in ("enable_rate_limit", "rate_limit_count", "rate_limit_seconds"):
				if key in spec:
					spec[key] = cint(spec[key])
			spec["allow_guest"] = 0
		if forced:
			spec["forced"] = forced
		if spec["operation"] == "create":
			spec.setdefault("script_type", "DocType Event")
			if spec["script_type"] not in SCRIPT_TYPES:
				raise CustomizationError(_("script_type must be DocType Event, API, Scheduler Event or Permission Query"),
					hints=[_("Workflow Task is not available to the agent")])
			script_type = spec["script_type"]
			if script_type == "DocType Event":
				_base.require(spec, ("reference_doctype", "doctype_event"), self.tool)
				keep = ("reference_doctype", "doctype_event")
			elif script_type == "API":
				_base.require(spec, ("api_method",), self.tool)
				keep = ("api_method", "enable_rate_limit", "rate_limit_count", "rate_limit_seconds")
			elif script_type == "Scheduler Event":
				_base.require(spec, ("event_frequency",), self.tool)
				keep = ("event_frequency", "cron_format")
			else:
				_base.require(spec, ("reference_doctype",), self.tool)
				keep = ("reference_doctype",)
			for key in ("reference_doctype", "doctype_event", "api_method", "event_frequency", "cron_format"):
				if key not in keep:
					spec.pop(key, None)
		elif spec["operation"] == "update" and spec.get("script_type") and spec["script_type"] not in SCRIPT_TYPES:
			raise CustomizationError(_("script_type must be DocType Event, API, Scheduler Event or Permission Query"))
		if spec.get("event_frequency") and spec["event_frequency"] not in FREQUENCIES:
			raise CustomizationError(_("event_frequency {0} is not available").format(spec["event_frequency"]),
				hints=[", ".join(FREQUENCIES)])
		if spec.get("cron_format") and spec.get("event_frequency") and spec["event_frequency"] != "Cron":
			raise CustomizationError(_("cron_format is used only with event_frequency Cron"))
		if spec.get("event_frequency") == "Cron" and spec["operation"] == "create" and not spec.get("cron_format"):
			raise CustomizationError(_("event_frequency Cron needs cron_format, e.g. \"30 6 * * *\" (every day at 06:30)"))
		if "sample_users" in spec:
			users = spec["sample_users"]
			if not isinstance(users, list) or not all(isinstance(u, str) for u in users) or len(users) > PQ_SAMPLE_USERS:
				raise CustomizationError(_("sample_users must be a list of at most {0} user ids").format(PQ_SAMPLE_USERS))
			missing = [u for u in users if not frappe.db.exists("User", u)]
			if missing:
				raise CustomizationError(_("Unknown user(s): {0}").format(", ".join(missing)))
		if spec.get("doctype_event") and spec["doctype_event"] not in EVENTS:
			raise CustomizationError(_("doctype_event {0} is not available").format(spec["doctype_event"]),
				hints=[", ".join(EVENTS)])
		if spec.get("api_method") and not API_METHOD_RE.match(spec["api_method"]):
			raise CustomizationError(_("api_method must match ^[a-z][a-z0-9_.]{2,80}$ (it becomes /api/method/<api_method>)"))
		return spec

	def normalize_create(self, spec, user):
		spec.setdefault("script_type", "DocType Event")
		super().normalize_create(spec, user)

	def default_name(self, spec):
		if spec.get("script_type") == "API":
			return spec.get("api_method") or spec.get("purpose")
		if spec.get("script_type") == "Scheduler Event":
			return f"{spec.get('purpose')} - {spec.get('event_frequency')}"[:140]
		if spec.get("script_type") == "Permission Query":
			return f"{spec.get('reference_doctype')} - Permission Query - {spec.get('purpose')}"[:140]
		return f"{spec.get('reference_doctype')} - {spec.get('doctype_event')} - {spec.get('purpose')}"[:140]

	def snapshot(self, spec):
		out = super().snapshot(spec)
		for key in self.optional_snapshot_fields:
			if out.get(key) in ("", None):
				out.pop(key, None)
		return out

	def record_values(self, spec):
		values = super().record_values(spec)
		if spec["operation"] in ("create", "update"):
			values["allow_guest"] = 0
		return values

	def check(self, spec, user):
		checks = _base.role_checks(user, self.required_roles, _("Frappe allows only Script Managers to save Server Scripts"))
		if not server_scripts_enabled():
			checks.append(Check("script.server_scripts_disabled", "critical",
				_("Server Scripts are disabled on this server (bench set-config -g server_script_enabled 1)")))
		checks += self.common_checks(spec)
		for key in spec.get("forced") or []:
			checks.append(Check("script.allow_guest", "warning", _("{0} is not allowed for agent scripts and was set to 0 (only logged-in users)").format(key)))
		merged, current = self.effective(spec)
		script_type = merged.get("script_type") or "DocType Event"
		if spec["operation"] != "disable" and script_type not in SCRIPT_TYPES:
			checks.append(Check("script.type", "critical", _("{0} scripts cannot be changed or enabled by the agent").format(script_type)))
		if spec["operation"] == "enable" and cint(current.get("allow_guest")):
			checks.append(Check("script.allow_guest", "critical", _("{0} allows guests and cannot be enabled by the agent").format(spec["name"])))
		reference = merged.get("reference_doctype")
		event = merged.get("doctype_event")
		impact = []

		if script_type == "Scheduler Event":
			checks += self._check_scheduler(merged, impact)
		elif script_type == "Permission Query":
			checks += self._check_permission_query_target(spec, reference, impact)
		elif script_type == "DocType Event":
			reason = _base.customizable_reason(reference, allow_custom=True) if reference else _("No reference_doctype given")
			if reason:
				checks.append(Check("script.doctype", "critical", reason))
			else:
				monthly = frappe.db.count(reference, {"creation": (">=", add_days(nowdate(), -30))})
				impact.append(_("Runs on {0} for every {1} (~{2} new in the last 30 days), for every user, also in imports and the REST API").format(event, reference, monthly))
				others = [n for n in self._event_scripts(reference, event) if n != spec.get("name")]
				if others:
					checks.append(Check("script.same_event", "info", _("Other enabled scripts on the same event: {0}").format(", ".join(others))))
		else:
			method = merged.get("api_method")
			taken = frappe.get_all("Server Script", filters={"api_method": method, "name": ("!=", spec.get("name") or "")}, pluck="name")
			if taken:
				checks.append(Check("script.api_taken", "critical", _("/api/method/{0} is already served by {1}").format(method, ", ".join(taken))))
			impact.append(_("Adds /api/method/{0}: any logged-in user can call it, and it runs with their permissions").format(method))
			if not cint(merged.get("enable_rate_limit")):
				checks.append(Check("script.rate_limit", "info", _("Consider enable_rate_limit for an API script")))

		code_ok = True
		if spec["operation"] in ("create", "update"):
			analysis = python_code.analyze(merged.get("script"), script_type, reference)
			checks += analysis
			code_ok = not any(c.level == "critical" for c in analysis)

		preview = {"name": spec.get("name"), "script_type": script_type, "reference_doctype": reference,
			"doctype_event": event, "api_method": merged.get("api_method"), "enabled": merged.get("enabled"),
			"code": self.code_preview(spec, "python")}
		if script_type == "Scheduler Event":
			preview["event_frequency"] = merged.get("event_frequency")
			preview["cron_format"] = merged.get("cron_format")
			preview["next_runs"] = self._next_runs(merged)
		if (script_type == "Permission Query" and code_ok and spec["operation"] in ("create", "update", "enable")
				and server_scripts_enabled() and reference and not any(c.level == "critical" for c in checks)):
			checks += self._preview_permission_query(spec, merged, reference, user, preview, impact)
		if (code_ok and spec["operation"] in ("create", "update") and script_type == "DocType Event"
				and reference and not _base.customizable_reason(reference, allow_custom=True)
				and server_scripts_enabled() and (event or "").startswith("Before")):
			try:
				trial = python_code.dry_run(merged.get("script"), reference, event, user)
			except Exception as e:
				trial = {"ran": False, "checks": [Check("py.dry_run_error", "warning", _("The trial run could not start: {0}").format(str(e)[:200]))]}
			checks += trial.pop("checks", [])
			preview["dry_run"] = trial
		return _base.build_report(checks, impact=impact, preview=preview, risk="high")

	# -- Scheduler Event -----------------------------------------------------------------
	def _check_scheduler(self, merged, impact):
		from frappe.utils.scheduler import is_scheduler_disabled

		checks = [Check("script.runs_as_admin", "warning",
			_("A Scheduler Event runs in the background as Administrator with no user: every read sees every record and every write skips permission checks. Nobody is shown its errors (they go to the Error Log)"))]
		frequency = merged.get("event_frequency")
		if not frequency:
			checks.append(Check("script.frequency", "critical", _("A Scheduler Event needs event_frequency: one of {0}").format(", ".join(FREQUENCIES))))
			return checks
		if frequency not in FREQUENCIES:
			checks.append(Check("script.frequency", "critical", _("event_frequency {0} is not available").format(frequency)))
			return checks
		cron = _cron_of(frequency, merged.get("cron_format"))
		if frequency == "Cron":
			from croniter import croniter

			if not cron:
				checks.append(Check("script.cron", "critical", _("event_frequency Cron needs cron_format")))
				return checks
			try:  # ScheduledJobType.validate: croniter(self.cron_format)
				runs = croniter(cron, now_datetime())
				first, second = runs.get_next(datetime), runs.get_next(datetime)
			except Exception as e:
				checks.append(Check("script.cron", "critical", _("{0} is not a valid cron expression: {1}").format(cron, str(e)[:200])))
				return checks
			if (second - first).total_seconds() < 15 * 60:
				checks.append(Check("script.cron_frequent", "warning", _("{0} runs more often than every 15 minutes").format(cron)))
		elif frequency == "All":
			checks.append(Check("script.cron_frequent", "warning", _("All runs on every scheduler tick (every few minutes)")))
		if frequency.endswith("Long"):
			checks.append(Check("script.long_queue", "info", _("{0} runs in the long queue").format(frequency)))
		if is_scheduler_disabled(verbose=False):
			checks.append(Check("script.scheduler_disabled", "warning", _("The scheduler is disabled on this site: the script will not run until it is enabled")))
		impact.append(_("Runs {0} ({1}) in the background as Administrator with no user, until it is disabled").format(frequency, cron))
		return checks

	@staticmethod
	def _next_runs(merged, count=3):
		from croniter import croniter

		cron = _cron_of(merged.get("event_frequency"), merged.get("cron_format"))
		if not cron:
			return []
		try:
			runs = croniter(cron, now_datetime())
			return [str(runs.get_next(datetime)) for _i in range(count)]
		except Exception:
			return []

	# -- Permission Query ------------------------------------------------------------------
	def _check_permission_query_target(self, spec, reference, impact):
		reason = _base.customizable_reason(reference, allow_custom=True) if reference else _("No reference_doctype given")
		if reason:
			return [Check("script.doctype", "critical", reason)]
		checks = []
		if frappe.get_meta(reference).istable:
			checks.append(Check("script.doctype", "critical", _("{0} is a child table: Permission Queries apply to the DocType that lists it").format(reference)))
		others = frappe.get_all("Server Script", filters={"script_type": "Permission Query", "reference_doctype": reference,
			"disabled": 0, "name": ("!=", spec.get("name") or "")}, pluck="name")
		if others:
			checks.append(Check("script.permission_query_taken", "critical",
				_("{0} already has the enabled Permission Query {1}; Frappe uses only one per DocType: update that one").format(reference, ", ".join(others))))
		hooks = frappe.get_hooks("permission_query_conditions", {})
		methods = list(hooks.get(reference, [])) + list(hooks.get("*", []))
		if methods:
			checks.append(Check("script.permission_hooks", "info",
				_("Apps already restrict {0} with {1}; this condition is added to theirs with AND").format(reference, ", ".join(methods))))
		checks.append(Check("script.permission_query_review", "warning",
			_("A Permission Query changes who sees which {0} everywhere (lists, reports, link fields, counts, the REST API). Review the condition for each kind of user").format(reference)))
		impact.append(_("Adds a condition to every list, report, link search and count of {0}, for every user, also in the REST API").format(reference))
		return checks

	@staticmethod
	def _sample_users(reference, requester):
		from ai_agent.customization.analyzers import sql as sql_analyzer

		plain, owner_only = sql_analyzer.read_roles(reference)
		roles = tuple(plain | owner_only)
		if not roles:
			return [requester]
		rows = frappe.db.sql(
			"""select u.name, max(hr.role = 'System Manager') as sm from `tabUser` u join `tabHas Role` hr on hr.parent = u.name
			where hr.parenttype = 'User' and u.enabled = 1 and u.user_type = 'System User'
			and u.name not in ('Administrator', 'Guest') and u.name in (
				select parent from `tabHas Role` where parenttype = 'User' and role in %(roles)s)
			group by u.name order by sm asc, u.creation desc limit %(limit)s""",
			{"roles": roles, "limit": PQ_SAMPLE_USERS},
		)
		return [r[0] for r in rows] or [requester]

	@contextlib.contextmanager
	def _with_permission_query(self, reference, script):
		"""Make this thread's list queries of ``reference`` use ``script`` as their Permission Query, without
		saving anything: get_server_script_map (server_script_utils and db_query's imported copy) and
		frappe.get_doc are wrapped so that only the current thread sees an unsaved Server Script. A lock keeps
		two previews of one process from interleaving their patches."""
		import frappe.model.db_query as db_query
		from frappe.core.doctype.server_script import server_script_utils

		ident = threading.get_ident()
		doc = frappe.new_doc("Server Script")
		doc.update({"script_type": "Permission Query", "reference_doctype": reference, "script": script, "disabled": 0})
		doc.name = PQ_PLACEHOLDER
		with _PREVIEW_LOCK:
			original_map, original_db_query_map = server_script_utils.get_server_script_map, db_query.get_server_script_map
			original_get_doc = frappe.get_doc

			def script_map():
				current = original_map()
				if threading.get_ident() != ident:
					return current
				return {**current, "permission_query": {**(current.get("permission_query") or {}), reference: PQ_PLACEHOLDER}}

			def get_doc(*args, **kwargs):
				if threading.get_ident() == ident and args[:2] == ("Server Script", PQ_PLACEHOLDER):
					return doc
				return original_get_doc(*args, **kwargs)

			server_script_utils.get_server_script_map = script_map
			db_query.get_server_script_map = script_map
			frappe.get_doc = get_doc
			try:
				yield
			finally:
				server_script_utils.get_server_script_map = original_map
				db_query.get_server_script_map = original_db_query_map
				frappe.get_doc = original_get_doc

	@staticmethod
	def _list_query(reference, user):
		"""(SQL of the list query, row count) for ``user``; the count runs under a 5-second statement limit."""
		sql = str(frappe.get_list(reference, fields=["name"], limit=20, order_by="creation desc", user=user, run=0))
		count_query = str(frappe.get_list(reference, fields=[{"COUNT": "*", "as": "n"}], user=user, run=0, order_by=""))
		rows = frappe.db.sql(f"SET STATEMENT max_statement_time=5 FOR {count_query}")
		return sql, cint(rows[0][0]) if rows else 0

	def _preview_permission_query(self, spec, merged, reference, user, preview, impact):
		from ai_agent.customization.analyzers import python_code

		script = merged.get("script")
		users = spec.get("sample_users") or self._sample_users(reference, user)
		checks, rows = [], []
		for sample in users:
			row = {"user": sample}
			rows.append(row)
			try:
				conditions = python_code.permission_conditions(script, sample, name=spec.get("name") or "preview")
			except Exception as e:
				checks.append(Check("script.permission_query_error", "critical",
					_("For {0} the script fails: {1}: {2}").format(sample, type(e).__name__, str(e)[:300])))
				continue
			if conditions in (None, ""):
				row["conditions"] = ""
				checks.append(Check("script.permission_query_open", "info", _("For {0} the script sets no condition: nothing changes for them").format(sample)))
				continue
			if not isinstance(conditions, str):
				checks.append(Check("script.permission_query_type", "critical", _("For {0} conditions is a {1}, not text").format(sample, type(conditions).__name__)))
				continue
			row["conditions"] = conditions
			checks += validate_condition(conditions, sample, reference)
		preview["permission_query"] = rows
		if any(c.level == "critical" for c in checks):
			return checks
		savepoint = "ai_customize_pq_preview"
		for row in rows:
			sample = row["user"]
			frappe.db.savepoint(savepoint)
			try:
				try:
					row["query_before"], row["count_before"] = self._list_query(reference, sample)
				except frappe.PermissionError:
					row["note"] = _("cannot read {0}").format(reference)
					continue
				except Exception as e:
					row["note"] = str(e)[:200]
				try:
					with self._with_permission_query(reference, script):
						row["query_after"], row["count_after"] = self._list_query(reference, sample)
				except Exception as e:
					checks.append(Check("script.permission_query_preview", "critical",
						_("With the condition the list query of {0} fails for {1}: {2}").format(reference, sample, str(e)[:300])))
					continue
				if row.get("count_before") is not None:
					impact.append(_("{0} would see {1} of the {2} {3} they see today").format(sample, row["count_after"], row["count_before"], reference))
					if row["count_after"] > row["count_before"]:
						checks.append(Check("script.permission_query_widens", "critical",
							_("For {0} the list grows from {1} to {2} rows").format(sample, row["count_before"], row["count_after"])))
			finally:
				frappe.db.rollback(save_point=savepoint)
		if not any(c.level == "critical" for c in checks):
			checks.append(Check("script.permission_query_preview", "ok", _("The list query of {0} was built and run with the condition for {1}").format(reference, ", ".join(users))))
		return checks

	@staticmethod
	def _event_scripts(reference, event):
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		return list((get_server_script_map().get(reference) or {}).get(event) or [])

	def after_execute(self, doc):
		frappe.client_cache.delete_value("server_script_map")
		if doc.reference_doctype:
			frappe.clear_cache(doctype=doc.reference_doctype)

	def extra_verify(self, spec, doc):
		from frappe.core.doctype.server_script.server_script_utils import get_server_script_map

		if doc.disabled:
			return []
		frappe.client_cache.delete_value("server_script_map")
		script_map = get_server_script_map()
		if doc.script_type == "Scheduler Event":
			job = frappe.db.get_value("Scheduled Job Type", {"server_script": doc.name}, ["name", "frequency", "cron_format", "stopped"], as_dict=True)
			if not job or job.frequency != doc.event_frequency or cint(job.stopped):
				return [Check("verify.scheduled_job", "critical", _("{0} has no running Scheduled Job Type with frequency {1}").format(doc.name, doc.event_frequency))]
			if doc.event_frequency == "Cron" and job.cron_format != doc.cron_format:
				return [Check("verify.scheduled_job", "critical", _("The Scheduled Job Type of {0} has another cron expression").format(doc.name))]
			return []
		if doc.script_type == "Permission Query":
			found = (script_map.get("permission_query") or {}).get(doc.reference_doctype) == doc.name
		elif doc.script_type == "API":
			found = (script_map.get("_api") or {}).get(doc.api_method) == doc.name
		else:
			found = doc.name in ((script_map.get(doc.reference_doctype) or {}).get(doc.doctype_event) or [])
		if not found:
			return [Check("verify.script_map", "critical", _("{0} is not registered for its event or API path").format(doc.name))]
		if cint(doc.allow_guest):
			return [Check("verify.allow_guest", "critical", _("{0} allows guests").format(doc.name))]
		return []

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "enable": _("Enable"), "disable": _("Disable")}[spec["operation"]]
		return _("{0} server script {1}").format(verb, spec.get("name"))


KIND = ServerScriptKind()
