"""Workspaces and their sidebars (Frappe 16.53): ``customize.request_workspace``.

Frappe 16.53 navigation, read here from Frappe's own code:

- **Workspace** (``frappe/desk/doctype/workspace``): the page. ``content`` is a JSON list of blocks whose names point
  at the *labels* of the child rows ``shortcuts``, ``links`` (Card Break + Link rows), ``number_cards`` and ``charts``.
  ``module`` is mandatory; an app's own pages are ``standard``.
- **Module sidebar**: a sidebar belongs to a module, not to a page. An app ships it (``Sidebar``); the site and each
  user arrange it in layers (``Custom Sidebar``, ``frappe/desk/doctype/custom_sidebar``). A shared page gets a row in
  its module's site layer (``add_to_sidebar``), a private one in its owner's (``Workspace.after_insert``). The old
  ``Workspace Sidebar`` is a read-only archive.
- **Desktop Icon** (``frappe/desk/doctype/desktop_icon``): the tile on /desk; it names the workspace and the desk
  routes it through the module sidebar.

A page with a sidebar of its own sits in a custom module of its own (Frappe makes the page a new custom module opens
on, and this kind fills it in); its sidebar is that module's site layer, saved through Frappe's ``save_site_sidebar``.
Without one, a page goes to the module asked for or Frappe's triage module. This kind writes only non-standard
records, so developer mode never exports them into an app, and it refuses to edit app-owned pages in place (copy them
with ``copy_from``). It never relies on ``frappe.flags.in_import`` (that flag also skips validations).
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check, CustomizationError
from ai_agent.customization.kinds import _base
from ai_agent.customization.kinds.workspace_spec import (  # noqa: F401 (current, sidebar_text: re-exported)
	_bit,
	_norm_cards,
	_norm_content,
	_norm_items,
	_norm_shortcuts,
	_norm_widgets,
	_sidebar_state,
	block_id,
	BLOCKS,
	current,
	desk_roles,
	icon_check,
	is_standard_sidebar,
	own_module,
	is_standard_workspace,
	ITEM_FIELDS,
	META_KEYS,
	OPERATIONS,
	place_blocks,
	ROLES,
	roles_that_open,
	section_labels,
	SECTIONS,
	short_list,
	SIDEBAR_KEYS,
	sidebar_text,
	target_exists,
	TOP_KEYS,
	URL_RE,
	workspace_name,
)

class WorkspaceKind:
	name = "workspace"
	setting = "allow_customize_dashboards"
	required_roles = ROLES

	def roles_for(self, spec):
		return ROLES

	# -- normalize ---------------------------------------------------------------------------
	def normalize(self, args, user):
		args = dict(args or {})
		_base.reject_unknown(args, TOP_KEYS, "customize.request_workspace")
		_base.require(args, ("operation", "name"), "customize.request_workspace")
		operation = args["operation"]
		if operation not in OPERATIONS:
			raise CustomizationError(_("operation must be one of: {0}").format(", ".join(OPERATIONS)))
		title = re.sub(r"\s+", " ", cstr(args.get("title") or args["name"])).strip()[:140]
		spec = {"operation": operation, "requested_by": user}
		if args.get("purpose"):
			spec["purpose"] = re.sub(r"\s+", " ", cstr(args["purpose"])).strip()[:140]

		if operation == "create":
			if not title:
				raise CustomizationError(_("Give the workspace a title"))
			public = _bit(1 if args.get("public") is None else args.get("public"))
			spec.update({"name": workspace_name(title, public, user), "title": title, "public": public})
			if not public:
				spec["for_user"] = user
				if args.get("sidebar") or cint(args.get("desktop_icon")):
					raise CustomizationError(_("A private workspace has no sidebar or desktop icon of its own"),
						hints=[_("Frappe lists a private workspace in its owner's own sidebar; send public: 1 for a shared one")])
			else:
				spec["desktop_icon"] = _bit(1 if args.get("desktop_icon") is None else args.get("desktop_icon"))
			if args.get("module"):
				module = cstr(args["module"]).strip()
				if not frappe.db.exists("Module Def", module):
					raise CustomizationError(_("There is no module {0}").format(module))
				spec["module"] = module
			if args.get("copy_from"):
				self._copy(spec, cstr(args["copy_from"]))
		else:
			name = cstr(args["name"]).strip()
			if not frappe.db.exists("Workspace", name):
				private = workspace_name(name, 0, user)
				if frappe.db.exists("Workspace", private):
					name = private
				else:
					raise CustomizationError(_("There is no workspace {0}").format(name),
						hints=[_("Use operation create, or the exact name from the Workspace list")])
			spec["name"] = name
			if args.get("copy_from"):
				raise CustomizationError(_("copy_from is only for operation create"))
			if args.get("title") and args["title"] != frappe.db.get_value("Workspace", name, "title"):
				raise CustomizationError(_("The title cannot change; create a new workspace (copy_from) and delete this one"))
			if operation in ("disable", "enable", "delete"):
				extra = set(args) - {"operation", "name", "purpose", "title"}
				if extra:
					raise CustomizationError(_("{0} takes only name and purpose").format(operation))
				return spec

		if args.get("icon") is not None:
			spec["icon"] = cstr(args["icon"])
		if args.get("indicator_color") is not None:
			options = (frappe.get_meta("Workspace").get_field("indicator_color").options or "").split("\n")
			if args["indicator_color"] and args["indicator_color"] not in options:
				raise CustomizationError(_("indicator_color must be one of {0}").format(", ".join(o for o in options if o)))
			spec["indicator_color"] = cstr(args["indicator_color"])
		if args.get("parent_page") is not None:
			spec["parent_page"] = cstr(args["parent_page"])
		if args.get("roles") is not None:
			roles = args["roles"]
			if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
				raise CustomizationError(_("roles must be a list of role names"))
			missing = [r for r in roles if not frappe.db.exists("Role", r)]
			if missing:
				raise CustomizationError(_("Unknown role(s): {0}").format(", ".join(missing)))
			spec["roles"] = sorted(set(roles))
		if args.get("shortcuts") is not None:
			spec["shortcuts"] = _norm_shortcuts(args["shortcuts"])
		if args.get("cards") is not None:
			spec["cards"] = _norm_cards(args["cards"])
		for key in ("number_cards", "charts"):
			if args.get(key) is not None:
				spec[key] = _norm_widgets(args[key], key)
		if args.get("content") is not None:
			spec["content"] = _norm_content(args["content"])
		if args.get("sidebar") is not None:
			sidebar = args["sidebar"]
			if not isinstance(sidebar, dict):
				raise CustomizationError(_("sidebar must be an object {items, header_icon}"))
			_base.reject_unknown(sidebar, SIDEBAR_KEYS, "sidebar")
			if cint(sidebar.get("remove")):
				spec["sidebar"] = {"remove": 1}
			else:
				_base.require(sidebar, ("items",), "sidebar")
				items = _norm_items(sidebar["items"])
				if not any(i.get("link_type") == "Workspace" and i.get("link_to") == spec["name"] for i in items):
					# the page itself is the first way in: a module opens on the first item of its sidebar
					items.insert(0, {"type": "Link", "label": "Home", "link_type": "Workspace", "link_to": spec["name"], "icon": "home"})
				spec["sidebar"] = {"items": items, "header_icon": cstr(sidebar.get("header_icon"))}
		if operation == "create" and spec.get("sidebar") and spec.get("public"):
			# Frappe 16.53: a sidebar belongs to a module, so a page with a sidebar of its own sits in a module of
			# its own (named after it unless the request names one)
			spec["module"] = spec.get("module") or spec["title"]
			if frappe.db.exists("Module Def", spec["module"]) and not own_module(spec["module"]):
				raise CustomizationError(_("{0} is not a custom module of the site: its sidebar is not this page's own").format(spec["module"]),
					hints=[_("Leave module empty: the page gets a module of its own named after it")])
		if operation == "update" and args.get("desktop_icon") is not None:
			spec["desktop_icon"] = _bit(args["desktop_icon"])
		if operation == "update" and not (set(spec) & set(SECTIONS + META_KEYS + ("content", "sidebar", "desktop_icon"))):
			raise CustomizationError(_("Nothing to change"), hints=[_("Send the sections to replace: shortcuts, cards, number_cards, charts, sidebar, icon, roles…")])
		return spec

	def _copy(self, spec, source):
		if not frappe.db.exists("Workspace", source):
			raise CustomizationError(_("There is no workspace {0} to copy").format(source))
		state = current(source)
		for key in SECTIONS + ("content",):
			spec[key] = state.get(key) or []
		for key in ("icon", "indicator_color"):
			spec[key] = state.get(key) or ""
		spec["roles"] = state.get("roles") or []
		sidebar = _sidebar_state(state.get("module"))
		if spec.get("public") and sidebar:
			items = [dict(i) for i in sidebar["items"]]
			for item in items:  # the copied Home link must open the copy
				if item.get("link_type") == "Workspace" and item.get("link_to") == source:
					item["link_to"] = spec["name"]
			spec["sidebar"] = {"items": items, "header_icon": sidebar.get("header_icon") or ""}
		spec["copied_from"] = source

	# -- snapshot ------------------------------------------------------------------------------
	def snapshot(self, spec):
		return current(spec["name"])

	# -- the effective record ---------------------------------------------------------------------
	def merged(self, spec, state=None):
		state = state or current(spec["name"])
		out = {k: state.get(k) for k in ("title", "public", "for_user", "module", "app", "icon", "indicator_color",
			"parent_page", "roles", "is_hidden") + SECTIONS}
		out["content"] = list(state.get("content") or [])
		out["public"] = state.get("public", 1)
		if spec["operation"] == "create":
			out.update({"title": spec["title"], "public": spec["public"], "for_user": spec.get("for_user") or "",
				"module": "", "app": "", "icon": "", "indicator_color": "", "parent_page": "", "roles": [], "is_hidden": 0})
			for key in SECTIONS:
				out[key] = []
			out["content"] = []
		for key in META_KEYS + SECTIONS:
			if key in spec:
				out[key] = spec[key]
		for key in SECTIONS:
			out[key] = out.get(key) or []
		if "content" in spec:
			out["content"] = list(spec["content"])
		elif spec["operation"] == "create":
			out["content"] = self._fresh_content(out)
		# blocks follow the sections (a no-op when the content already matches them)
		for block_type, (section, _key, _col) in BLOCKS.items():
			if section in spec:
				out["content"] = place_blocks(out["content"], block_type, section_labels(section, out[section]))
		sidebar = state.get("sidebar")
		if "sidebar" in spec:
			sidebar = None if spec["sidebar"].get("remove") else {"title": out.get("title") or spec["name"], "standard": 0, "app": "",
				"header_icon": spec["sidebar"].get("header_icon") or (sidebar or {}).get("header_icon") or out.get("icon") or "",
				"items": spec["sidebar"]["items"]}
		out["sidebar"] = sidebar
		out["desktop_icon"] = spec.get("desktop_icon", 1 if state.get("desktop_icon") else 0)
		return out

	def _fresh_content(self, out):
		content = []

		def header(text):
			content.append({"id": block_id(), "type": "header", "data": {"text": f'<span class="h4"><b>{frappe.utils.escape_html(text)}</b></span>', "col": 12}})

		for block_type, title in (("number_card", None), ("chart", None), ("shortcut", _("Your Shortcuts")), ("card", _("Reports & Masters"))):
			section, key, col = BLOCKS[block_type]
			labels = section_labels(section, out[section])
			if not labels:
				continue
			if title:
				header(title)
			for label in labels:
				content.append({"id": block_id(), "type": block_type, "data": {key: label, "col": col}})
		return content

	# -- check -----------------------------------------------------------------------------------
	def check(self, spec, user):
		checks = _base.role_checks(user, ROLES, _("Frappe lets only Workspace Managers save public workspaces and delete sidebars"))
		state = current(spec["name"])
		op = spec["operation"]
		preview = {"name": spec["name"], "operation": op}
		impact = []

		if op == "create":
			if state["exists"]:
				checks.append(Check("workspace.name_taken", "critical", _("A workspace named {0} already exists; use operation update").format(spec["name"])))
			from frappe.desk.utils import slug

			taken = [dt for dt in ("Page", "DocType", "Workspace") if any(slug(n) == slug(spec["name"])
				for n in frappe.get_all(dt, filters={"name": ("like", spec["name"].replace("-", "_").replace(" ", "_"))}, pluck="name"))]
			if taken and not state["exists"]:
				checks.append(Check("workspace.route_conflict", "critical", _("{0} is already a route of a {1}: pick another title").format(spec["name"], ", ".join(taken))))
			if spec["public"] and state.get("sidebar"):
				checks.append(Check("workspace.sidebar_taken", "critical", _("A sidebar named {0} already exists; pick another title or update that sidebar").format(spec["title"])))
			if spec.get("desktop_icon") and state.get("desktop_icon"):
				checks.append(Check("workspace.icon_taken", "warning", _("A desktop icon named {0} already exists: it is kept and no new icon is made").format(spec["title"])))
		elif state["exists"] and is_standard_workspace(state) and (op in ("delete", "disable", "enable") or set(spec) & set(SECTIONS + META_KEYS + ("content",))):
			checks.append(Check("workspace.standard", "critical",
				_("{0} belongs to the app {1}: it is not edited in place (developer mode would write the change into the app, migrate would overwrite it). Create a copy: operation create with copy_from {0}").format(
					spec["name"], state.get("app") or state.get("module"))))
		if op == "update" and "sidebar" in spec and state["exists"] and not own_module(state.get("module")):
			checks.append(Check("workspace.shared_sidebar", "critical",
				_("{0} lists in the sidebar of the module {1}, which other pages share: arrange it in Desk (the sidebar's Customize), or create a copy with a sidebar of its own (operation create with copy_from and sidebar)").format(
					spec["name"], state.get("module") or "-")))
		if op in ("disable", "enable") and state["exists"] and not state.get("public"):
			checks.append(Check("workspace.private", "critical", _("A private workspace cannot be hidden; delete it instead")))

		if op in ("disable", "enable", "delete"):
			if op == "delete":
				checks += self._dry_delete(spec, state)
				impact.append(_("Removes the workspace {0}{1}").format(spec["name"],
					_(", its sidebar and desktop icon") if state.get("sidebar") and not is_standard_sidebar(state["sidebar"]) else ""))
			else:
				impact.append(_("{0} is {1} for everyone but Workspace Managers").format(spec["name"], _("hidden") if op == "disable" else _("shown")))
			preview["sidebar"] = {"before": sidebar_text((state.get("sidebar") or {}).get("items"))}
			return _base.build_report(checks, impact=impact, preview=preview, risk="low")

		merged = self.merged(spec, state)
		audience, audience_label = self._audience(merged, spec)
		checks += self._check_targets(merged, spec, audience, audience_label)
		checks += self._check_icons(merged, spec)
		checks += self._check_labels(merged)
		if merged.get("parent_page") and not frappe.db.exists("Workspace", merged["parent_page"]):
			checks.append(Check("workspace.parent_page", "critical", _("There is no workspace {0} to nest under").format(merged["parent_page"])))
		if merged.get("sidebar") and not any(i.get("type") == "Link" for i in merged["sidebar"]["items"]):
			checks.append(Check("workspace.sidebar_empty", "warning", _("The sidebar has no links: Frappe hides a sidebar without a link (boot.get_sidebar_items)")))
		if not any(c.level == "critical" for c in checks):
			checks += self._dry_save(spec, merged, state, user)

		preview.update({
			"title": merged.get("title"), "public": merged.get("public"), "roles": merged.get("roles") or [],
			"shortcuts": section_labels("shortcuts", merged["shortcuts"]),
			"cards": {c["label"]: [l["label"] for l in c["links"]] for c in merged["cards"]},
			"number_cards": section_labels("number_cards", merged["number_cards"]),
			"charts": section_labels("charts", merged["charts"]),
			"blocks": [f"{b['type']}:{(b.get('data') or {}).get(BLOCKS.get(b['type'], (0, 'text'))[1], '')}"[:80] for b in merged["content"]],
		})
		if merged.get("sidebar") or state.get("sidebar"):
			preview["sidebar"] = {"before": sidebar_text((state.get("sidebar") or {}).get("items")),
				"after": sidebar_text((merged.get("sidebar") or {}).get("items"))}
		if spec.get("copied_from"):
			preview["copied_from"] = spec["copied_from"]
		impact.append(audience_label)
		if merged.get("sidebar") and ("sidebar" in spec or op == "create"):
			impact.append(_("The left menu {0} shows {1} item(s) on this workspace and on every page it links").format(
				merged["sidebar"]["title"], len(merged["sidebar"]["items"])))
		if op == "create" and merged.get("public") and spec.get("desktop_icon") and not state.get("desktop_icon"):
			impact.append(_("Adds a desktop icon {0} for every user who can see one of its sidebar links").format(spec["title"]))
		return _base.build_report(checks, impact=impact, preview=preview, risk="low")

	def _audience(self, merged, spec):
		if not merged.get("public"):
			owner = merged.get("for_user") or spec.get("requested_by")
			return {"user": owner}, _("Only {0} sees this private workspace").format(owner)
		roles = merged.get("roles") or []
		if roles:
			return {"roles": set(roles)}, _("Shown to the roles {0}").format(short_list(roles))
		return {"roles": set(desk_roles())}, _("Shown to every desk user")

	def _targets(self, merged):
		out = []
		for s in merged["shortcuts"]:
			if s["type"] != "URL":
				out.append((_("Shortcut {0}").format(s["label"]), s["type"], s["link_to"]))
			else:
				out.append((_("Shortcut {0}").format(s["label"]), "URL", s.get("url")))
		for card in merged["cards"]:
			for link in card["links"]:
				out.append((_("Card {0} › {1}").format(card["label"], link["label"]), link["link_type"], link["link_to"]))
		for item in (merged.get("sidebar") or {}).get("items") or []:
			if item.get("type") == "Link":
				target = item.get("url") if item.get("link_type") == "URL" else item.get("link_to")
				out.append((_("Sidebar {0}").format(item.get("label")), item.get("link_type"), target))
		return out

	def _check_targets(self, merged, spec, audience, audience_label):
		checks = []
		seen = set()
		for where, link_type, link_to in self._targets(merged):
			if link_type == "URL":
				if not URL_RE.match(cstr(link_to)):
					checks.append(Check("workspace.url", "critical", _("{0}: a URL starts with https://, http:// or / ({1})").format(where, link_to)))
				continue
			if link_type == "Workspace" and link_to == spec["name"]:
				continue  # the workspace itself (created in this request)
			if not target_exists(link_type, link_to):
				checks.append(Check("workspace.link_missing", "critical", _("{0}: there is no {1} {2}").format(where, link_type, link_to)))
				continue
			if (link_type, link_to) in seen:
				continue
			seen.add((link_type, link_to))
			checks += self._audience_check(where, link_type, link_to, audience)
		for section, doctype, widget in (("number_cards", "Number Card", _("Number card")), ("charts", "Dashboard Chart", _("Chart"))):
			for row in merged[section]:
				if not frappe.db.exists(doctype, row["name"]):
					checks.append(Check("workspace.link_missing", "critical", _("{0}: there is no {1} {2}").format(widget, doctype, row["name"])))
					continue
				source = frappe.db.get_value(doctype, row["name"], ["document_type", "report_name"], as_dict=True)
				if source.document_type and frappe.db.exists("DocType", source.document_type):
					checks += self._audience_check(_("{0} {1}").format(widget, row["name"]), "DocType", source.document_type, audience)
				elif source.report_name and frappe.db.exists("Report", source.report_name):
					checks += self._audience_check(_("{0} {1}").format(widget, row["name"]), "Report", source.report_name, audience)
		return self._cap_audience(checks)

	def _cap_audience(self, checks, limit=8):
		audience = [c for c in checks if c.code == "workspace.link_audience"]
		if len(audience) <= limit:
			return checks
		others = [c for c in checks if c.code != "workspace.link_audience"]
		return others + audience[:limit] + [Check("workspace.link_audience", "warning",
			_("{0} more links are hidden from some of the audience").format(len(audience) - limit))]

	def _audience_check(self, where, link_type, link_to, audience):
		if audience.get("user"):
			user = audience["user"]
			ok = True
			if link_type == "DocType":
				ok = frappe.has_permission(link_to, "read", user=user)
			elif link_type in ("Report", "Page"):
				allowed = roles_that_open(link_type, link_to)
				ok = allowed is None or user == "Administrator" or bool(allowed & set(frappe.get_roles(user)))
			if not ok:
				return [Check("workspace.link_audience", "warning", _("{0}: {1} cannot open {2} {3}; Frappe hides it").format(where, user, link_type, link_to))]
			return []
		allowed = roles_that_open(link_type, link_to)
		if allowed is None:
			return []
		blocked = set(audience.get("roles") or ()) - allowed - {"System Manager"}
		if not blocked:
			return []
		return [Check("workspace.link_audience", "warning", _("{0}: the roles {1} cannot open {2} {3}; Frappe hides it from users who hold only those").format(
			where, short_list(blocked), link_type, link_to))]

	def _check_icons(self, merged, spec):
		checks = []
		for where, icon in [(_("Workspace icon"), spec.get("icon")), (_("Sidebar header icon"), ((spec.get("sidebar") or {}).get("header_icon")))] + \
				[(_("Sidebar {0}").format(i.get("label")), i.get("icon")) for i in ((spec.get("sidebar") or {}).get("items") or [])] + \
				[(_("Shortcut {0}").format(s["label"]), s.get("icon")) for s in spec.get("shortcuts") or []] + \
				[(_("Card {0}").format(c["label"]), c.get("icon")) for c in spec.get("cards") or []]:
			found = icon_check(icon, where)
			if found:
				checks.append(found)
		return checks

	def _check_labels(self, merged):
		checks = []
		for section, what in (("shortcuts", _("shortcut")), ("cards", _("card")), ("number_cards", _("number card")), ("charts", _("chart"))):
			labels = section_labels(section, merged[section])
			dupes = sorted({x for x in labels if labels.count(x) > 1})
			if dupes:
				checks.append(Check("workspace.duplicate", "critical", _("Two {0}s share the label {1}: blocks find their widget by label").format(what, ", ".join(dupes))))
		for block in merged["content"]:
			spec_ = BLOCKS.get(block.get("type"))
			if not spec_:
				continue
			label = (block.get("data") or {}).get(spec_[1])
			if label not in section_labels(spec_[0], merged[spec_[0]]):
				checks.append(Check("workspace.block", "critical", _("The {0} block {1} has no matching row in {2}").format(block["type"], label, spec_[0])))
		items = (merged.get("sidebar") or {}).get("items") or []
		labels = [i.get("label") for i in items if i.get("type") == "Link"]
		dupes = sorted({x for x in labels if labels.count(x) > 1})
		if dupes:
			checks.append(Check("workspace.duplicate", "warning", _("Sidebar links share a label: {0}").format(", ".join(dupes))))
		return checks

	def _dry_save(self, spec, merged, state, user):
		collect = _base.capture_messages()
		try:
			with _base.dry_run(), _base.as_user(user):
				self._write(spec, merged, state, approval=None)
		except Exception as exc:
			return [Check("workspace.frappe", "critical", _("Frappe refuses the change: {0}").format(_base.frappe_error(exc, collect())))]
		collect()
		return [Check("workspace.frappe", "ok", _("Frappe's own validation passes (dry run, rolled back)"))]

	def _dry_delete(self, spec, state):
		if not state["exists"]:
			return [Check("workspace.missing", "critical", _("There is no workspace {0}").format(spec["name"]))]
		collect = _base.capture_messages()
		try:
			with _base.dry_run():
				self._delete(spec, state)
		except Exception as exc:
			return [Check("workspace.frappe", "critical", _("Frappe refuses the change: {0}").format(_base.frappe_error(exc, collect())))]
		collect()
		return [Check("workspace.frappe", "ok", _("The workspace can be removed (dry run, rolled back)"))]

	# -- writing ------------------------------------------------------------------------------------
	def _write(self, spec, merged, state, approval=None):
		op = spec["operation"]
		touches_workspace = op == "create" or bool(set(spec) & set(SECTIONS + META_KEYS + ("content",)))
		if touches_workspace:
			if op == "create":
				module = self._module_for(spec, merged)
				if frappe.db.exists("Workspace", spec["name"]) and frappe.db.get_value("Workspace", spec["name"], "module") == module:
					# Frappe 16.53 makes the page a new custom module opens on, named after the module: this is it
					doc = frappe.get_doc("Workspace", spec["name"])
				else:
					doc = frappe.new_doc("Workspace")
					from frappe.desk.doctype.workspace.workspace import last_sequence_id

					doc.sequence_id = last_sequence_id(doc) + 1
				doc.update({"label": spec["name"], "title": merged["title"], "public": merged["public"],
					"for_user": merged.get("for_user") or "", "type": "Workspace", "standard": 0, "module": module})
			else:
				doc = frappe.get_doc("Workspace", spec["name"])
			doc.icon = merged.get("icon") or ("grid" if op == "create" else doc.icon)
			doc.indicator_color = merged.get("indicator_color") or None
			doc.parent_page = merged.get("parent_page") or ""
			doc.set("roles", [{"role": r} for r in merged.get("roles") or []])
			doc.set("shortcuts", [dict(s) for s in merged["shortcuts"]])
			links = []
			for card in merged["cards"]:
				links.append({"type": "Card Break", "label": card["label"], "icon": card.get("icon") or None,
					"description": card.get("description") or None, "hidden": card.get("hidden") or 0, "link_count": len(card["links"])})
				links += [{"type": "Link", **link} for link in card["links"]]
			doc.set("links", links)
			doc.set("number_cards", [{"number_card_name": r["name"], "label": r.get("label") or r["name"]} for r in merged["number_cards"]])
			doc.set("charts", [{"chart_name": r["name"], "label": r.get("label") or r["name"]} for r in merged["charts"]])
			doc.content = json.dumps(merged["content"])
			if op == "create" and doc.is_new():
				doc.insert()
				if merged["public"]:
					# Frappe 16.53: a shared page is reachable through a row in its module's sidebar, which the
					# creating path adds (a private one gets its rows in Workspace.after_insert)
					from frappe.desk.doctype.workspace.workspace import add_to_sidebar

					add_to_sidebar(doc)
			else:
				doc.save()
		if merged.get("public"):
			if "sidebar" in spec:
				module = spec.get("module") if op == "create" else state.get("module")
				self._write_sidebar(module, merged.get("sidebar"), merged.get("title") or spec["name"])
			if "desktop_icon" in spec:
				self._write_icon(spec, merged, state)

	@staticmethod
	def _module_for(spec, merged):
		"""The page's module (Workspace.module is mandatory from Frappe 16.53): the one asked for or, for a page
		with a sidebar of its own, one of its own (made here as a custom module); else Frappe's triage module."""
		module = spec.get("module")
		if not module:
			return _site_module(merged.get("for_user"))
		if not frappe.db.exists("Module Def", module):
			made = frappe.get_doc({"doctype": "Module Def", "module_name": module, "custom": 1})
			made.flags.page_icon = merged.get("icon") or None  # Frappe puts it on the page the module opens on
			made.insert()
		return module

	@staticmethod
	def _write_sidebar(module, wanted, title):
		"""The page's own sidebar is the site layer of its module's sidebar (Frappe 16.53 Custom Sidebar), saved
		through Frappe's own call for it (a Workspace Manager's save for everyone)."""
		from frappe.desk.doctype.custom_sidebar import custom_sidebar

		if not own_module(module):
			raise CustomizationError(_("{0} is not a module of this page's own: its sidebar is shared").format(module or "-"))
		if wanted is None:
			custom_sidebar.reset_site_sidebar(module)
			return
		rows = []
		for item in wanted["items"]:
			row = {k: item.get(k) for k in ITEM_FIELDS if item.get(k) not in (None, "")}
			row.setdefault("collapsible", 1)
			rows.append({**row, "added": 1})
		custom_sidebar.save_site_sidebar(module, items=rows, label=title, header_icon=wanted.get("header_icon") or "")

	def _write_icon(self, spec, merged, state):
		label = merged.get("title") or spec["name"]
		existing = state.get("desktop_icon")
		if not spec.get("desktop_icon"):
			if existing and not existing.get("standard") and not existing.get("app"):
				frappe.delete_doc("Desktop Icon", label, ignore_missing=True)
			return
		if existing:
			return
		icon = frappe.new_doc("Desktop Icon")
		# Frappe 16.53 (desktop_icon.add_workspace_to_desktop): the icon names the workspace and the desk routes it
		# through the module sidebar; link_type is an inert column naming the archived Workspace Sidebar, hence
		# ignore_links
		icon.update({"label": label, "icon_type": "Link", "link_type": "Workspace Sidebar", "link_to": spec["name"],
			"icon": merged.get("icon") or None, "standard": 0, "app": None})
		icon.insert(ignore_links=True)
		# A non-standard icon is listed only for its owner and for Administrator-owned icons
		# (desktop_icon.py get_desktop_icons): own it by Administrator, as Frappe's own non-standard icons are,
		# so every user who can see the sidebar gets the tile. The approval keeps who made it.
		frappe.db.set_value("Desktop Icon", icon.name, "owner", "Administrator", update_modified=False)
		from frappe.desk.doctype.desktop_icon.desktop_icon import clear_desktop_icons_cache

		clear_desktop_icons_cache()

	def _delete(self, spec, state):
		name = spec["name"]
		icon = state.get("desktop_icon")
		if icon and not icon.get("standard") and not icon.get("app"):
			frappe.delete_doc("Desktop Icon", icon["name"], ignore_missing=True)
		# Frappe 16.53 Workspace.on_trash takes the page out of every sidebar layer that lists it and drops its icon
		frappe.delete_doc("Workspace", name)
		module = own_module(state.get("module"))
		if module and state.get("sidebar"):
			from frappe.desk.doctype.custom_sidebar import custom_sidebar

			custom_sidebar.reset_site_sidebar(module)
			# the page's own module goes with it when nothing else lives in it
			if not any(frappe.db.exists(dt, {"module": module}) for dt in ("Workspace", "DocType", "Report", "Page", "Print Format")):
				frappe.delete_doc("Module Def", module, ignore_missing=True)

	# -- execute / verify ---------------------------------------------------------------------------
	def execute(self, spec, user, approval=None):
		state = current(spec["name"])
		op = spec["operation"]
		with _base.as_user(user):
			if op == "create":
				self._write(spec, self.merged(spec, state), state, approval)
				undo = {"operation": "delete", "name": spec["name"]}
			elif op == "delete":
				self._delete(spec, state)
				undo = self.restore_spec(state)
			elif op in ("disable", "enable"):
				frappe.db.set_value("Workspace", spec["name"], "is_hidden", 1 if op == "disable" else 0)
				icon = state.get("desktop_icon")
				if icon and not icon.get("standard"):
					frappe.db.set_value("Desktop Icon", icon["name"], "hidden", 1 if op == "disable" else 0)
				from frappe.desk.doctype.desktop_icon.desktop_icon import clear_desktop_icons_cache

				clear_desktop_icons_cache()
				undo = {"operation": "enable" if op == "disable" else "disable", "name": spec["name"]}
			else:
				merged = self.merged(spec, state)
				self._write(spec, merged, state, approval)
				undo = self._update_undo(spec, state)
		frappe.cache.delete_key("bootinfo")
		return {"targets": [{"doctype": "Workspace", "name": spec["name"]}], "undo": undo}

	def _update_undo(self, spec, state):
		undo = {"operation": "update", "name": spec["name"], "purpose": _("undo of {0}").format(_base.approval_name() or "")}
		for key in META_KEYS + SECTIONS:
			if key in spec:
				undo[key] = state.get(key) if state.get(key) is not None else ([] if key in SECTIONS + ("roles",) else "")
		if set(spec) & set(SECTIONS + ("content",)):
			undo["content"] = state.get("content") or []
		if "sidebar" in spec:
			before = state.get("sidebar")
			undo["sidebar"] = {"items": before["items"], "header_icon": before.get("header_icon") or ""} if before else {"remove": 1}
		if "desktop_icon" in spec:
			undo["desktop_icon"] = 1 if state.get("desktop_icon") else 0
		return undo

	def restore_spec(self, state):
		"""A create spec that rebuilds ``state`` (the undo of a delete)."""
		spec = {"operation": "create", "name": state["name"], "title": state.get("title") or state["name"],
			"public": state.get("public", 1), "requested_by": state.get("for_user") or frappe.session.user,
			"purpose": _("restore {0}").format(state["name"])}
		if not spec["public"]:
			spec["for_user"] = state.get("for_user")
		for key in META_KEYS + SECTIONS + ("content",):
			spec[key] = state.get(key) if state.get(key) is not None else ""
		if state.get("module"):
			spec["module"] = state["module"]
		if state.get("sidebar") and spec["public"] and not is_standard_sidebar(state["sidebar"]):
			spec["sidebar"] = {"items": state["sidebar"]["items"], "header_icon": state["sidebar"].get("header_icon") or ""}
		spec["desktop_icon"] = 1 if state.get("desktop_icon") and spec["public"] else 0
		return spec

	def verify(self, spec, result):
		state = current(spec["name"])
		op = spec["operation"]
		checks = []
		needs_page = op in ("create", "disable", "enable") or bool(set(spec) & set(SECTIONS + META_KEYS + ("content",)))
		if op == "delete":
			if state["exists"]:
				checks.append(Check("verify.deleted", "critical", _("{0} still exists").format(spec["name"])))
		elif needs_page and not state["exists"]:
			checks.append(Check("verify.exists", "critical", _("Workspace {0} does not exist").format(spec["name"])))
		elif op in ("disable", "enable"):
			if state.get("is_hidden") != (1 if op == "disable" else 0):
				checks.append(Check("verify.hidden", "critical", _("{0} is not {1}").format(spec["name"], _("hidden") if op == "disable" else _("shown"))))
		else:
			for key in SECTIONS + ("roles",):
				if key in spec and json.dumps(state.get(key), sort_keys=True) != json.dumps(spec[key] if key != "roles" else sorted(spec[key]), sort_keys=True):
					checks.append(Check("verify.section", "critical", _("{0} differs from the approved change").format(key)))
			if "sidebar" in spec:
				wanted = None if spec["sidebar"].get("remove") else spec["sidebar"]["items"]
				got = (state.get("sidebar") or {}).get("items")
				if json.dumps(wanted, sort_keys=True) != json.dumps(got, sort_keys=True):
					checks.append(Check("verify.sidebar", "critical", _("The sidebar differs from the approved one")))
			if op == "create" and not spec.get("public") and spec.get("for_user"):
				listed = frappe.db.exists("Sidebar Item", {"parenttype": "Custom Sidebar", "link_type": "Workspace", "link_to": spec["name"]})
				if not listed:
					checks.append(Check("verify.my_workspaces", "warning", _("The workspace is not listed in {0}'s sidebar").format(spec["for_user"])))
		if not checks:
			checks.append(Check("verify.workspace", "ok", _("{0} is in place").format(spec["name"])))
		return checks

	def summary(self, spec):
		verb = {"create": _("Create"), "update": _("Update"), "disable": _("Hide"), "enable": _("Show"), "delete": _("Delete")}[spec["operation"]]
		private = "" if spec.get("public", 1) else _(" (private)")
		return _("{0} workspace {1}{2}").format(verb, spec["name"], private)

	def get_source(self, name, user=None):
		if not frappe.db.exists("Workspace", name):
			raise frappe.DoesNotExistError(_("There is no workspace {0}").format(name))
		state = current(name)
		state["kind"] = self.name
		state["standard"] = is_standard_workspace(state) if state["exists"] else False
		if state.get("sidebar"):
			state["sidebar"]["preview"] = sidebar_text(state["sidebar"]["items"])
		return state


def _site_module(for_user=None):
	"""Where a workspace goes when the request names no module (Workspace.module is mandatory from Frappe 16.53):
	Frappe's own answer, a site module no app owns ("Private" for a private page, "Custom Workspaces" otherwise)."""
	from frappe.desk.doctype.workspace.workspace import triage_module

	return triage_module(for_user or None)


KIND = WorkspaceKind()
