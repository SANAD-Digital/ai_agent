# On the version-15 branch only: what Frappe 15 lacks and this app's code names, so the code stays the
# same as on main and later merges stay clean (BRANCHES.md). Loaded from `ai_agent/__init__.py`.
#
# Each change is an addition Frappe 16 already has, made for the whole process; apps on the same site
# see it too. Every one checks first, so it is harmless when another app (whatsapp_next) made it already.

from __future__ import annotations

import typing

import frappe
import frappe.permissions
import frappe.tests
import frappe.utils.data
from frappe.database.query import Engine
from frappe.model import base_document
from frappe.model.db_query import DatabaseQuery
from frappe.utils.redis_wrapper import RedisWrapper


def _tests_getattr(name: str):
	"""`from frappe.tests import IntegrationTestCase` (16) -> `FrappeTestCase` (15), on first use."""
	if name == "IntegrationTestCase":
		from frappe.tests.utils import FrappeTestCase

		return FrappeTestCase
	raise AttributeError(name)


if not hasattr(frappe.tests, "__getattr__"):
	frappe.tests.__getattr__ = _tests_getattr


class _ClientCache:
	"""16's `frappe.client_cache` (a request-local cache in front of Redis) -> 15's `frappe.cache`, looked up
	on each call: `frappe.cache` is set when a site is initialised, after this module may be imported."""

	def __getattr__(self, name):
		return getattr(frappe.cache, name)


if not hasattr(frappe, "client_cache"):
	frappe.client_cache = _ClientCache()

# 16 names the permission types `std_rights`; 15 calls the same tuple `rights`.
if not hasattr(frappe.permissions, "std_rights"):
	frappe.permissions.std_rights = frappe.permissions.rights


def _reserved_keywords():
	from frappe.model.document import Document

	return Document._reserved_keywords


# 16's `RESERVED_KEYWORDS`: the attribute names a field may not take (15 keeps them on Document).
if not hasattr(base_document, "RESERVED_KEYWORDS"):
	base_document.RESERVED_KEYWORDS = _reserved_keywords()

# 16's `TimespanOptions`: the spans `get_timespan_date_range` knows, as 15's own function lists them.
if not hasattr(frappe.utils.data, "TimespanOptions"):
	frappe.utils.data.TimespanOptions = typing.Literal[
		"last 7 days", "last 14 days", "last 30 days", "last 90 days", "last week", "last month", "last quarter",
		"last 6 months", "last year", "yesterday", "today", "tomorrow", "this week", "this month", "this quarter",
		"this year", "next 7 days", "next 14 days", "next 30 days", "next week", "next month", "next quarter",
		"next 6 months", "next year",
	]


def _aggregate_field(field):
	"""16's `{"COUNT": "name", "as": "n"}` -> 15's "count(`name`) as `n`"."""
	if not isinstance(field, dict):
		return field
	alias = field.get("as")
	((func, column),) = ((k, v) for k, v in field.items() if k != "as")
	column = "*" if column == "*" else f"`{column}`"
	return f"{func.lower()}({column})" + (f" as `{alias}`" if alias else "")


if not getattr(DatabaseQuery.execute, "_takes_aggregate_dicts", False):
	_execute = DatabaseQuery.execute

	def _execute_with_aggregates(self, fields=None, *args, **kwargs):
		if isinstance(fields, list | tuple) and any(isinstance(f, dict) for f in fields):
			fields = [_aggregate_field(f) for f in fields]
		return _execute(self, fields, *args, **kwargs)

	_execute_with_aggregates._takes_aggregate_dicts = True
	DatabaseQuery.execute = _execute_with_aggregates


def _set_value_like_16(self, key, val, user=None, expires_in_sec=None, shared=False):
	"""Frappe 15 skips this request's local copy when a value has an expiry, so a read after it in the same
	request or job returned the old value, or nothing (a cancel flag, a once-only claim). 16 always keeps it."""
	result = _set_value(self, key, val, user=user, expires_in_sec=expires_in_sec, shared=shared)
	frappe.local.cache[self.make_key(key, user, shared)] = val
	return result


if RedisWrapper.set_value.__name__ == "set_value":  # not wrapped yet (whatsapp_next makes the same change)
	_set_value = RedisWrapper.set_value
	RedisWrapper.set_value = _set_value_like_16


def _get_query_with_permissions(self, table, *args, ignore_permissions=True, user=None, ignore_user_permissions=False,
		parent_doctype=None, **kwargs):
	"""16's `frappe.qb.get_query(..., ignore_permissions=False, user=...)` applies the user's read access; 15's
	takes neither argument and applies none. Here the rows are limited to the names Frappe 15's own `get_list`
	gives that user (role permissions, User Permissions, permission query hooks, if_owner, shares), and a user
	who may not read the DocType gets its PermissionError, as on 16. 15's `get_list` cannot skip User
	Permissions, so `ignore_user_permissions` is not honoured: the stricter result."""
	query = _get_query(self, table, *args, **kwargs)
	if ignore_permissions:
		return query
	from frappe.query_builder import DocType
	from pypika.terms import LiteralValue

	doctype = table if isinstance(table, str) else self.doctype
	permitted = frappe.get_list(doctype, fields=["name"], user=user, parent_doctype=parent_doctype, order_by="",
		limit_page_length=0, run=0)
	return query.where(DocType(doctype).name.isin(LiteralValue(f"({permitted})")))


if not getattr(Engine.get_query, "_takes_permissions", False):
	_get_query = Engine.get_query
	_get_query_with_permissions._takes_permissions = True
	Engine.get_query = _get_query_with_permissions


def _prepare_args_with_parent(self):
	"""A child-table read naming its parent (`get_list(child, parent_doctype=parent)`), as 16 builds it:

	- a filter on a parent field joins the parent by `parent.name = child.parent`; 15 joined it as if the parent
	  were another child table (`parent.parenttype = child`), an SQL error;
	- the rows are limited to those whose parent document the user may read (User Permissions, permission query
	  hooks, if_owner, shares); 15 checked only the role, so a user restricted to one company read another's lines.
	"""
	args = _prepare_args(self)
	parent = getattr(self, "parent_doctype", None)
	if not parent or not frappe.get_meta(self.doctype).istable:
		return args
	from frappe.model.db_query import cast_name

	child, table = self.tables[0], f"`tab{parent}`"
	if table in self.tables[1:]:
		wrong = (f" {self.join} {table} on ({table}.parenttype = {frappe.db.escape(self.doctype)}"
			f" and {table}.parent = {cast_name(f'{child}.name')})")
		right = f" {self.join} {table} on ({table}.name = {child}.parent)"
		args.tables = args.tables.replace(wrong, right)
		self._user_tables = self._user_tables.replace(wrong, right)
	if not self.flags.ignore_permissions:
		permitted = frappe.get_list(parent, fields=["name"], user=self.user, order_by="", limit_page_length=0, run=0)
		readable = f"{child}.parent in ({permitted})"
		args.conditions = f"({args.conditions}) and {readable}" if args.conditions else readable
	return args


if not getattr(DatabaseQuery.prepare_args, "_joins_parent", False):
	_prepare_args = DatabaseQuery.prepare_args
	_prepare_args_with_parent._joins_parent = True
	DatabaseQuery.prepare_args = _prepare_args_with_parent


def _filter_through_link(self, f):
	"""16 filters through a Link field: `["account.root_type", "=", "Income"]` on GL Entry means the GL Entries
	whose account is an Income account. 15 reads `account.root_type` as a column name, an SQL error. Here the
	linked DocType is joined the way 15 joins it for a `link.field` column (`append_link_table`: its read
	permission is checked and, unless the query ignores permissions, its row-level conditions sit in the join),
	and the filter is put on that join."""
	if isinstance(f, list | tuple) and len(f) in (3, 4) and isinstance(f[-3], str) and "." in f[-3]:
		doctype = f[0] if len(f) == 4 else self.doctype
		link, field = f[-3].split(".", 1)
		df = frappe.get_meta(doctype).get_field(link) if doctype == self.doctype else None
		if df and df.fieldtype == "Link":
			alias = self.append_link_table(df.options, link).table_alias
			table = f"`tab{df.options}`"
			joined = table in self.tables
			condition = _prepare_filter_condition(self, [df.options, field, f[-2], f[-1]])
			if not joined and table in self.tables:
				self.tables.remove(table)
			return condition.replace(f"{table}.", f"{alias}.")
	return _prepare_filter_condition(self, f)


if not getattr(DatabaseQuery.prepare_filter_condition, "_through_links", False):
	_prepare_filter_condition = DatabaseQuery.prepare_filter_condition
	_filter_through_link._through_links = True
	DatabaseQuery.prepare_filter_condition = _filter_through_link


def _get_value_like_16(self, key, generator=None, user=None, expires=False, shared=False, *, use_local_cache=True):
	"""16's `get_value(..., use_local_cache=False)`: read Redis itself, not this request's copy, so a job sees a flag
	another process set after it first looked (the chat's Stop button). 15 has no such argument."""
	if use_local_cache:
		return _get_value(self, key, generator, user, expires, shared)
	frappe.local.cache.pop(self.make_key(key, user, shared), None)
	return _get_value(self, key, generator, user, True, shared)  # expires=True: 15 then keeps no local copy


if "use_local_cache" not in RedisWrapper.get_value.__code__.co_varnames:
	_get_value = RedisWrapper.get_value
	RedisWrapper.get_value = _get_value_like_16


def _only_for_like_16(roles, message=False):
	"""15's `frappe.only_for` lets every user through while tests run (`flags.in_test`), so a test cannot show that
	an endpoint refuses a user without the role; 16 dropped that exception. Outside tests both behave alike."""
	if frappe.flags.in_test and frappe.session.user != "Administrator":
		roles = (roles,) if isinstance(roles, str) else roles
		if set(roles).isdisjoint(frappe.get_roles()):
			if not message:
				raise frappe.PermissionError
			frappe.throw(frappe._("This action is only allowed for {}").format(
				", ".join(frappe.bold(frappe._(role)) for role in roles)), frappe.PermissionError, frappe._("Not Permitted"))
		return
	return _only_for(roles, message)


if not getattr(frappe.only_for, "_checks_in_tests", False):
	_only_for = frappe.only_for
	_only_for_like_16._checks_in_tests = True
	frappe.only_for = _only_for_like_16


from frappe.custom.doctype.customize_form.customize_form import CustomizeForm  # noqa: E402


def _allow_property_change_with_meta(self, prop, meta_df, df, meta=None):
	"""16 passes the DocType's meta as a fourth argument; 15's takes three."""
	return _allow_property_change(self, prop, meta_df, df)


if CustomizeForm.allow_property_change.__code__.co_argcount == 4:  # self, prop, meta_df, df
	_allow_property_change = CustomizeForm.allow_property_change
	CustomizeForm.allow_property_change = _allow_property_change_with_meta

# List View Settings on 15 has no show_tags or disable_scrolling
NO_LIST_FLAGS = ("show_tags", "disable_scrolling")


def _app_workspace(row):
	"""App-owned on 15: a public page of an app's module (developer mode exports it into the app; migrate restores it)."""
	module = row.get("module")
	return bool(frappe.utils.cint(row.get("public")) and module and not frappe.db.get_value("Module Def", module, "custom"))


def _app_workspaces(limit=1):
	custom = frappe.get_all("Module Def", filters={"custom": 1}, pluck="name")
	filters = [["public", "=", 1], ["module", "is", "set"], ["module", "not in", custom or [""]]]
	return frappe.get_all("Workspace", filters=filters, pluck="name", limit=limit)


def _customization_on_15():
	"""What the customization tools offer on 15.

	- `customize.request_workspace` is built on Frappe 16's navigation (module sidebars in layers, desktop icons);
	  15 has neither, so the tool is not offered and the model is never told about it (BRANCHES.md, known gaps).
	  Workspaces still get number cards and charts through `customize.request_dashboard`.
	- `customize.request_list_settings` offers only the flags 15's List View Settings has."""
	from ai_agent.customization import registry

	registry.TOOL_SPECS[:] = [s for s in registry.TOOL_SPECS if s["kind"] != "workspace"]
	for table in (registry.KINDS, registry.KIND_SETTINGS, registry.KIND_ROLES):
		table.pop("workspace", None)
	for tool in [t for t, kind in registry.REQUEST_TOOLS.items() if kind == "workspace"]:
		del registry.REQUEST_TOOLS[tool]
	registry.NOT_OFFERED.add("customize.request_workspace")

	# where customize.request_dashboard places widgets: 15 has no `standard` on Workspace and no triage module
	from ai_agent.customization.kinds import workspace, workspace_spec

	workspace_spec.is_standard_workspace = _app_workspace
	workspace_spec.app_workspaces = _app_workspaces
	workspace._site_module = lambda for_user=None: ""
	registry.ALL_TOOLS = registry.READ_TOOLS + tuple(registry.REQUEST_TOOLS) + (registry.UNDO_TOOL,)

	from ai_agent.customization.kinds import list_settings

	list_settings.FLAGS = tuple(f for f in list_settings.FLAGS if f not in NO_LIST_FLAGS)
	for spec in registry.TOOL_SPECS:
		if spec["kind"] == "list_settings":
			spec["get_fields"] = [f for f in spec.get("get_fields") or [] if f not in NO_LIST_FLAGS]
			flags = ((spec.get("input_schema") or {}).get("properties") or {}).get("settings") or {}
			for f in NO_LIST_FLAGS:
				(flags.get("properties") or {}).pop(f, None)


_customization_on_15()

from ai_agent import compat_v15_oauth  # noqa: E402, F401  (the OAuth pieces AI tools sign in with)
