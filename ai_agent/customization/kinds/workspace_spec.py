"""The workspace request as data: the keys and values it accepts, their normalization, and what the site has now
(the workspace, its sidebar and desktop icon, the icons and roles the desk knows). Used by workspace.WorkspaceKind."""

from __future__ import annotations

import difflib
import json
import os
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base

TOP_KEYS = ("operation", "name", "title", "public", "icon", "indicator_color", "parent_page", "roles",
	"shortcuts", "cards", "number_cards", "charts", "content", "sidebar", "desktop_icon", "copy_from", "purpose")
OPERATIONS = ("create", "update", "disable", "enable", "delete")
SECTIONS = ("shortcuts", "cards", "number_cards", "charts")
META_KEYS = ("icon", "indicator_color", "parent_page", "roles")
SHORTCUT_KEYS = ("label", "type", "link_to", "doc_view", "url", "color", "stats_filter", "format", "icon", "kanban_board")
SHORTCUT_TYPES = ("DocType", "Report", "Page", "Dashboard", "URL")
DOC_VIEWS = ("", "List", "Report Builder", "Dashboard", "Tree", "New", "Calendar", "Kanban", "Image")
CARD_KEYS = ("label", "icon", "description", "hidden", "links")
LINK_KEYS = ("label", "link_type", "link_to", "onboard", "dependencies", "only_for", "is_query_report", "hidden")
LINK_TYPES = ("DocType", "Page", "Report")
WIDGET_KEYS = ("name", "label")
SIDEBAR_KEYS = ("items", "header_icon", "remove")
ITEM_KEYS = ("type", "label", "link_type", "link_to", "url", "icon", "child", "indent", "collapsible", "keep_closed",
	"show_arrow", "filters", "route_options", "items")
ITEM_TYPES = ("Link", "Section Break", "Spacer")
ITEM_LINK_TYPES = ("DocType", "Page", "Report", "Workspace", "Dashboard", "URL")
ITEM_FIELDS = ("type", "label", "link_type", "link_to", "url", "icon", "child", "indent", "collapsible", "keep_closed",
	"show_arrow", "filters", "route_options")
BLOCK_TYPES = ("header", "paragraph", "spacer", "shortcut", "card", "number_card", "chart", "quick_list",
	"custom_block", "onboarding")
# block type -> (section, data key, default col)
BLOCKS = {"shortcut": ("shortcuts", "shortcut_name", 3), "card": ("cards", "card_name", 4),
	"number_card": ("number_cards", "number_card_name", 4), "chart": ("charts", "chart_name", 12)}
ROLES = ("System Manager", "Workspace Manager")
IGNORED_AUDIENCE_ROLES = {"Administrator", "Guest", "All", "Desk User"}
EVERYONE_ROLES = {"All", "Desk User"}  # every desk user holds them
MAX_ITEMS = 120
URL_RE = re.compile(r"^(https?://[^\s<>\"']+|/[^\s<>\"']*)$", re.I)
_ICONS = {}


def icon_names():
	"""Every symbol id the desk can draw: the sprites in ``app_include_icons`` (lucide, timeless, espresso,
	app sprites; frappe/hooks.py:41-57), as ``frappe.utils.icon`` resolves them (utils.js:1398-1420):
	``es-…`` ids as they are, the others without their ``icon-`` prefix."""
	key = tuple(frappe.get_hooks("app_include_icons") or ())
	if key in _ICONS:
		return _ICONS[key]
	names = set()
	for path in key:
		full = _asset_file(cstr(path))
		if not full:
			continue
		with open(full, encoding="utf-8") as handle:
			for symbol in re.findall(r"<symbol[^>]*\bid=\"([^\"]+)\"", handle.read()):
				names.add(symbol[5:] if symbol.startswith("icon-") else symbol)
	_ICONS.clear()
	_ICONS[key] = names
	return names


def _asset_file(path):
	"""``/assets/<app>/…`` on disk: the built assets folder, else the app's own public folder (a site whose assets
	were never built or linked still knows its icons)."""
	full = os.path.join(frappe.local.sites_path, path.lstrip("/"))
	if os.path.exists(full):
		return full
	parts = path.lstrip("/").split("/", 2)
	if len(parts) == 3 and parts[0] == "assets":
		try:
			full = frappe.get_app_path(parts[1], "public", parts[2])
		except Exception:
			return None
		return full if os.path.exists(full) else None
	return None


def is_emoji(text):
	text = cstr(text)
	return bool(text) and len(text) <= 8 and all(ord(ch) > 0x2000 for ch in text)


def icon_check(icon, where):
	if not icon or is_emoji(icon) or icon in icon_names():
		return None
	close = difflib.get_close_matches(icon, sorted(icon_names()), n=4, cutoff=0.6)
	hint = _(" Close names: {0}").format(", ".join(close)) if close else ""
	return Check("workspace.icon", "critical", _("{0}: there is no icon named {1} in Frappe's icon set.{2}").format(where, icon, hint))


def desk_roles():
	"""Roles held by enabled desk (System) users: the audience of a workspace without roles."""
	rows = frappe.db.sql(
		"""select distinct hr.role from `tabHas Role` hr join `tabUser` u on u.name = hr.parent
		where hr.parenttype = 'User' and u.enabled = 1 and u.user_type = 'System User'""",
		pluck=True,
	)
	return sorted(set(rows) - IGNORED_AUDIENCE_ROLES)


def roles_that_open(link_type, link_to):
	"""Roles that can open a target, or None when every desk user can (Frappe's own rules:
	DocType read perms, Report/Page ``roles`` tables, Workspace ``roles``)."""
	from ai_agent.customization.analyzers import sql as sql_analyzer

	if link_type == "DocType":
		plain, owner_only = sql_analyzer.read_roles(link_to)
		roles = plain | owner_only
		return None if roles & EVERYONE_ROLES else roles
	if link_type == "Report":
		roles = set(frappe.get_all("Has Role", filters={"parenttype": "Report", "parent": link_to}, pluck="role"))
		if roles:
			return None if roles & EVERYONE_ROLES else roles
		ref = frappe.db.get_value("Report", link_to, "ref_doctype")
		return roles_that_open("DocType", ref) if ref and frappe.db.exists("DocType", ref) else None
	if link_type == "Page":
		roles = set(frappe.get_all("Has Role", filters={"parenttype": "Page", "parent": link_to}, pluck="role"))
		return None if not roles or roles & EVERYONE_ROLES else roles
	if link_type == "Workspace":
		roles = set(frappe.get_all("Has Role", filters={"parenttype": "Workspace", "parent": link_to}, pluck="role"))
		return None if not roles or roles & EVERYONE_ROLES else roles
	return None


def short_list(values, limit=6):
	values = sorted(values)
	if len(values) <= limit:
		return ", ".join(values)
	return _("{0} and {1} more").format(", ".join(values[:limit]), len(values) - limit)


def target_exists(link_type, link_to):
	if link_type == "URL":
		return True
	doctype = {"Dashboard": "Dashboard", "Workspace": "Workspace", "Page": "Page", "Report": "Report", "DocType": "DocType"}.get(link_type)
	return bool(doctype and link_to and frappe.db.exists(doctype, link_to))


def is_standard_workspace(row):
	"""App-owned: it has a module/app (saving it in developer mode exports it into the app, and migrate
	re-imports it over local edits)."""
	return bool(row.get("module") or row.get("app"))


def is_standard_sidebar(row):
	return bool(cint(row.get("standard")) or row.get("app"))


def block_id():
	return frappe.generate_hash(length=10)


def place_blocks(content, block_type, labels, col=None):
	"""Blocks of ``block_type`` become exactly ``labels`` in that order: existing blocks keep their slots
	(and their width), new ones go right after the last slot of that type (or at the end), missing ones go."""
	_section, key, default_col = BLOCKS[block_type]
	existing = {}
	for block in content:
		if block.get("type") == block_type:
			existing.setdefault(cstr((block.get("data") or {}).get(key)), block)
	wanted = [existing.get(label) or {"id": block_id(), "type": block_type, "data": {key: label, "col": col or default_col}}
		for label in labels]
	slots = [i for i, block in enumerate(content) if block.get("type") == block_type]
	if not slots:
		return list(content) + wanted
	out, queue = [], list(wanted)
	for i, block in enumerate(content):
		if block.get("type") == block_type:
			if queue:
				out.append(queue.pop(0))
		else:
			out.append(block)
		if i == slots[-1]:
			out.extend(queue)
			queue = []
	return out


def section_labels(section, rows):
	if section in ("number_cards", "charts"):
		return [r.get("label") or r.get("name") for r in rows]
	return [r.get("label") for r in rows]


# -- reading a workspace ---------------------------------------------------------------------------


def _cards_from_links(rows):
	cards, current = [], None
	for row in rows:
		if row.type == "Card Break":
			current = {"label": row.label, "icon": row.icon or "", "description": row.description or "",
				"hidden": cint(row.hidden), "links": []}
			cards.append(current)
		elif current is not None:
			link = {"label": row.label, "link_type": row.link_type, "link_to": row.link_to}
			for key in ("onboard", "is_query_report", "hidden"):
				if cint(row.get(key)):
					link[key] = 1
			for key in ("dependencies", "only_for"):
				if row.get(key):
					link[key] = row.get(key)
			current["links"].append(link)
	return cards


def _sidebar_state(title):
	if not title or not frappe.db.exists("Workspace Sidebar", title):
		return None
	doc = frappe.get_doc("Workspace Sidebar", title)
	items = []
	for row in sorted(doc.items, key=lambda r: r.idx):
		item = {"type": row.type or "Link", "label": row.label or ""}
		for key in ("link_type", "link_to", "url", "icon", "filters", "route_options"):
			if row.get(key) and not (key == "link_type" and item["type"] != "Link"):
				item[key] = row.get(key)
		for key in ("child", "indent", "keep_closed", "show_arrow"):
			if cint(row.get(key)):
				item[key] = 1
		if not cint(row.collapsible):
			item["collapsible"] = 0
		items.append(item)
	return {"title": doc.name, "standard": cint(doc.standard), "app": doc.app or "", "for_user": doc.for_user or "",
		"header_icon": doc.header_icon or "", "items": items}


def _icon_state(label):
	if not label or not frappe.db.exists("Desktop Icon", label):
		return None
	row = frappe.db.get_value("Desktop Icon", label, ["name", "standard", "app", "icon", "hidden", "link_type", "link_to",
		"icon_type", "owner"], as_dict=True)
	return {k: (cint(v) if k in ("standard", "hidden") else cstr(v)) for k, v in row.items()}


def current(name):
	"""Everything this kind manages for ``name``, as comparable JSON (also the snapshot)."""
	state = {"name": name, "exists": bool(name and frappe.db.exists("Workspace", name))}
	if state["exists"]:
		doc = frappe.get_doc("Workspace", name)
		try:
			content = json.loads(doc.content or "[]")
		except ValueError:
			content = []
		state.update({
			"title": doc.title, "public": cint(doc.public), "for_user": doc.for_user or "", "module": doc.module or "",
			"app": doc.app or "", "icon": doc.icon or "", "indicator_color": doc.indicator_color or "",
			"parent_page": doc.parent_page or "", "roles": sorted(r.role for r in doc.roles), "is_hidden": cint(doc.is_hidden),
			"shortcuts": [{k: cstr(s.get(k)) for k in SHORTCUT_KEYS if s.get(k)} for s in doc.shortcuts],
			"cards": _cards_from_links(doc.links),
			"number_cards": [{"name": r.number_card_name, "label": r.label or r.number_card_name} for r in doc.number_cards],
			"charts": [{"name": r.chart_name, "label": r.label or r.chart_name} for r in doc.charts],
			"content": content,
		})
	title = state.get("title") or name
	state["sidebar"] = _sidebar_state(title) if state.get("public", 1) else None
	state["desktop_icon"] = _icon_state(title) if state.get("public", 1) else None
	return json.loads(json.dumps(state, default=str))


def workspace_name(title, public, user):
	return title if public else f"{title}-{user}"


# -- normalize helpers -----------------------------------------------------------------------------------


def _bit(value):
	return 1 if cint(value) else 0


def _norm_shortcuts(value):
	if not isinstance(value, list):
		raise CustomizationError(_("shortcuts must be a list"))
	out = []
	for i, row in enumerate(value, 1):
		where = f"shortcuts[{i}]"
		if not isinstance(row, dict):
			raise CustomizationError(_("{0} must be an object").format(where))
		_base.reject_unknown(row, SHORTCUT_KEYS, where)
		_base.require(row, ("label", "type"), where)
		if row["type"] not in SHORTCUT_TYPES:
			raise CustomizationError(_("{0}.type must be one of {1}").format(where, ", ".join(SHORTCUT_TYPES)))
		if row["type"] == "URL":
			_base.require(row, ("url",), where)
		else:
			_base.require(row, ("link_to",), where)
		if cstr(row.get("doc_view")) not in DOC_VIEWS:
			raise CustomizationError(_("{0}.doc_view must be one of {1}").format(where, ", ".join(v for v in DOC_VIEWS if v)))
		clean = {k: cstr(v) for k, v in row.items() if v not in (None, "")}
		if isinstance(row.get("stats_filter"), (list, dict)):
			clean["stats_filter"] = json.dumps(row["stats_filter"])
		out.append(clean)
	return out


def _norm_cards(value):
	if not isinstance(value, list):
		raise CustomizationError(_("cards must be a list"))
	out = []
	for i, card in enumerate(value, 1):
		where = f"cards[{i}]"
		if not isinstance(card, dict):
			raise CustomizationError(_("{0} must be an object").format(where))
		_base.reject_unknown(card, CARD_KEYS, where)
		_base.require(card, ("label",), where)
		links = card.get("links") or []
		if not isinstance(links, list):
			raise CustomizationError(_("{0}.links must be a list").format(where))
		clean_links = []
		for j, link in enumerate(links, 1):
			lw = f"{where}.links[{j}]"
			if not isinstance(link, dict):
				raise CustomizationError(_("{0} must be an object").format(lw))
			_base.reject_unknown(link, LINK_KEYS, lw)
			_base.require(link, ("label", "link_type", "link_to"), lw)
			if link["link_type"] not in LINK_TYPES:
				raise CustomizationError(_("{0}.link_type must be one of {1}").format(lw, ", ".join(LINK_TYPES)))
			clean = {"label": cstr(link["label"]), "link_type": link["link_type"], "link_to": cstr(link["link_to"])}
			for key in ("onboard", "is_query_report", "hidden"):
				if cint(link.get(key)):
					clean[key] = 1
			for key in ("dependencies", "only_for"):
				if link.get(key):
					clean[key] = cstr(link[key])
			clean_links.append(clean)
		out.append({"label": cstr(card["label"]), "icon": cstr(card.get("icon")), "description": cstr(card.get("description")),
			"hidden": _bit(card.get("hidden")), "links": clean_links})
	return out


def _norm_widgets(value, where):
	if not isinstance(value, list):
		raise CustomizationError(_("{0} must be a list of names or {{name, label}} objects").format(where))
	out = []
	for i, row in enumerate(value, 1):
		if isinstance(row, str):
			row = {"name": row}
		if not isinstance(row, dict):
			raise CustomizationError(_("{0}[{1}] must be a name or an object").format(where, i))
		_base.reject_unknown(row, WIDGET_KEYS, f"{where}[{i}]")
		_base.require(row, ("name",), f"{where}[{i}]")
		out.append({"name": cstr(row["name"]), "label": cstr(row.get("label") or row["name"])})
	return out


def _norm_items(value):
	"""Sidebar items, flat (Frappe's storage) or nested (a Section Break with ``items``)."""
	if not isinstance(value, list):
		raise CustomizationError(_("sidebar.items must be a list"))
	out = []

	def add(row, where, in_section):
		if not isinstance(row, dict):
			raise CustomizationError(_("{0} must be an object").format(where))
		_base.reject_unknown(row, ITEM_KEYS, where)
		kind = row.get("type") or "Link"
		if kind not in ITEM_TYPES:
			raise CustomizationError(_("{0}.type must be one of {1}").format(where, ", ".join(ITEM_TYPES)))
		item = {"type": kind, "label": cstr(row.get("label"))}
		if kind == "Link":
			_base.require(row, ("label",), where)
			link_type = row.get("link_type") or ("URL" if row.get("url") else "DocType")
			if link_type not in ITEM_LINK_TYPES:
				raise CustomizationError(_("{0}.link_type must be one of {1}").format(where, ", ".join(ITEM_LINK_TYPES)))
			item["link_type"] = link_type
			if link_type == "URL":
				_base.require(row, ("url",), where)
				item["url"] = cstr(row["url"]).strip()
			else:
				_base.require(row, ("link_to",), where)
				item["link_to"] = cstr(row["link_to"])
			for key in ("filters", "route_options"):
				if row.get(key) not in (None, "", [], {}):
					item[key] = row[key] if isinstance(row[key], str) else json.dumps(row[key])
		elif kind == "Section Break":
			_base.require(row, ("label",), where)
			item["indent"] = 1 if row.get("indent") is None else _bit(row.get("indent"))
		if row.get("icon"):
			item["icon"] = cstr(row["icon"])
		for key in ("child", "keep_closed", "show_arrow"):
			if cint(row.get(key)):
				item[key] = 1
		if kind == "Link" and in_section:
			item["child"] = 1
		if row.get("collapsible") is not None and not cint(row.get("collapsible")):
			item["collapsible"] = 0
		if kind == "Section Break" and item.get("indent") == 0:
			item.pop("indent")
		out.append(item)
		nested = row.get("items")
		if nested is not None:
			if kind != "Section Break" or not isinstance(nested, list):
				raise CustomizationError(_("{0}.items: only a Section Break holds nested items (a list)").format(where))
			for j, child in enumerate(nested, 1):
				add(child, f"{where}.items[{j}]", True)

	for i, row in enumerate(value, 1):
		add(row, f"sidebar.items[{i}]", False)
	if len(out) > MAX_ITEMS:
		raise CustomizationError(_("At most {0} sidebar items per request ({1} sent)").format(MAX_ITEMS, len(out)))
	return out


def _norm_content(value):
	if not isinstance(value, list):
		raise CustomizationError(_("content must be a list of blocks"))
	out = []
	for i, block in enumerate(value, 1):
		if not isinstance(block, dict) or block.get("type") not in BLOCK_TYPES or not isinstance(block.get("data", {}), dict):
			raise CustomizationError(_("content[{0}] must be {{id, type, data}} with type one of {1}").format(i, ", ".join(BLOCK_TYPES)))
		out.append({"id": cstr(block.get("id")) or block_id(), "type": block["type"], "data": block.get("data") or {}})
	return out


def sidebar_text(items, header=None):
	"""The sidebar as an indented list (the card preview)."""
	lines = [f"[{header}]"] if header else []
	for item in items or []:
		kind = item.get("type") or "Link"
		icon = f" ({item['icon']})" if item.get("icon") else ""
		if kind == "Section Break":
			lines.append(f"▾ {item.get('label')}{icon}")
		elif kind == "Spacer":
			lines.append("  ·")
		else:
			target = item.get("url") if item.get("link_type") == "URL" else item.get("link_to")
			indent = "    " if cint(item.get("child")) else ""
			lines.append(f"{indent}- {item.get('label')} → {item.get('link_type')}: {target}{icon}")
	return "\n".join(lines)


# -- the kind --------------------------------------------------------------------------------------------
