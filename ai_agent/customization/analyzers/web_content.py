"""Web Page and Web Form content checks: unsafe HTML, CSS and URLs, Jinja limited to safe names, and a rendering
of the page as a given user. Used by kinds.web.WebKind."""

from __future__ import annotations

import contextlib
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, cstr

from ai_agent.customization.contract import Check
from ai_agent.customization.kinds import _base

UNSAFE_HTML = ("html.script", "html.event_handler", "html.javascript_url", "html.embed", "html.form_external",
	"html.meta_refresh", "html.base", "html.password")
URL_ATTRS = ("href", "src", "action", "formaction", "xlink:href", "data", "poster", "background", "srcset", "cite")

# Jinja: names of the render globals that are fine on a public page (formatting only)
SAFE_JINJA_NAMES = {"_", "range", "dict", "list", "tuple", "set", "sorted", "min", "max", "sum", "abs", "all", "any",
	"bool", "enumerate", "isinstance", "cycler", "joiner", "namespace", "lipsum", "is_rtl", "scrub", "get_dom_id",
	"html2text", "bundled_asset", "include_style", "include_icons"}
SAFE_FRAPPE_ATTRS = {"utils", "format", "format_value", "format_date", "date_format", "time_format", "number_format",
	"bold", "get_url", "lang", "_"}
# The formatting helpers of frappe.utils a public page may call (no file, request, user or database access).
SAFE_UTILS = {"fmt_money", "formatdate", "format_date", "format_datetime", "format_time", "format_duration",
	"global_date_format", "getdate", "get_datetime", "nowdate", "today", "now", "now_datetime", "add_days",
	"add_months", "add_to_date", "date_diff", "flt", "cint", "cstr", "rounded", "money_in_words", "in_words",
	"pretty_date", "comma_and", "comma_or", "escape_html", "strip_html", "strip_html_tags", "get_url", "markdown",
	"md_to_html", "ceil", "floor", "get_first_day", "get_last_day"}


def _bit(value):
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, str) and value.strip().lower() in ("true", "yes"):
		return 1
	return 1 if cint(value) else 0


def _scrub(text):
	from frappe.website.utils import cleanup_page_name

	return cleanup_page_name(cstr(text)).replace("_", "-")


def _site_host():
	return (urlparse(frappe.utils.get_url()).hostname or "").lower()


def _is_external(url):
	url = cstr(url).strip()
	if not url:
		return False
	if url.startswith("//"):
		url = "http:" + url
	parsed = urlparse(url)
	if parsed.scheme not in ("http", "https") or not parsed.hostname:
		return False
	return parsed.hostname.lower() != _site_host()


def _bad_scheme(url):
	compact = re.sub(r"[\s\x00-\x1f]+", "", cstr(url)).lower()
	return compact.startswith(("javascript:", "vbscript:", "data:text/html", "data:image/svg"))


# -- HTML / CSS / Jinja analysis -----------------------------------------------------------------

PREVIEW_CHARS = 6000


class _HTMLScan(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.findings = []  # (code, detail, line)
		self.links = []
		self.styles = []
		self._in_style = False

	def _add(self, code, detail):
		self.findings.append((code, detail, self.getpos()[0]))

	def handle_starttag(self, tag, attrs):
		attrs = [(cstr(k).lower(), cstr(v)) for k, v in attrs]
		values = dict(attrs)
		if tag == "script":
			self._add("html.script", values.get("src") or "<script>")
			if values.get("src") and _is_external(values["src"]):
				self.links.append(values["src"])
		elif tag in ("iframe", "frame", "frameset", "object", "embed", "applet", "portal"):
			self._add("html.embed", f"<{tag}>")
		elif tag == "base":
			self._add("html.base", "<base>")
		elif tag == "meta" and values.get("http-equiv", "").lower() == "refresh":
			self._add("html.meta_refresh", values.get("content", ""))
		elif tag == "form":
			action = values.get("action", "")
			if _is_external(action):
				self._add("html.form_external", action)
		elif tag == "input" and values.get("type", "").lower() == "password":
			self._add("html.password", "<input type=password>")
		elif tag == "style":
			self._in_style = True
		for name, value in attrs:
			if name.startswith("on"):
				self._add("html.event_handler", f"<{tag} {name}>")
			elif name in URL_ATTRS:
				for url in (value.split(",") if name == "srcset" else [value]):
					url = url.strip().split(" ")[0] if name == "srcset" else url.strip()
					if _bad_scheme(url):
						self._add("html.javascript_url", f"<{tag} {name}>")
					elif _is_external(url) and url not in self.links:
						self.links.append(url)
			elif name == "style":
				self.styles.append((value, self.getpos()[0]))

	handle_startendtag = handle_starttag

	def handle_endtag(self, tag):
		if tag == "style":
			self._in_style = False

	def handle_data(self, data):
		if self._in_style:
			self.styles.append((data, self.getpos()[0]))


def scan_html(html, allow_scripts=False, where=""):
	"""Checks + external links of one piece of public HTML."""
	scan = _HTMLScan()
	try:
		scan.feed(cstr(html))
		scan.close()
	except Exception as e:  # HTMLParser is lenient; this is a guard only
		return [Check("html.parse", "critical", _("The HTML cannot be read: {0}").format(str(e)[:200]))], []
	checks = []
	messages = {
		"html.script": _("{0}: a <script> runs code in every visitor's browser ({1})"),
		"html.event_handler": _("{0}: an inline event handler runs code in every visitor's browser ({1})"),
		"html.javascript_url": _("{0}: a javascript:/data: address runs code when clicked ({1})"),
		"html.embed": _("{0}: {1} embeds another page or plugin inside the site"),
		"html.form_external": _("{0}: a form posts what visitors type to another site ({1})"),
		"html.meta_refresh": _("{0}: <meta refresh> sends visitors elsewhere ({1})"),
		"html.base": _("{0}: {1} changes where every link on the page points"),
		"html.password": _("{0}: {1} asks visitors for a password (phishing pattern)"),
	}
	level = "warning" if allow_scripts else "critical"
	for code, detail, line in scan.findings:
		checks.append(Check(code, level, messages[code].format(where, detail), line))
	links = list(scan.links)
	for text, line in scan.styles:
		css_checks, css_links = scan_css(text, allow_scripts, where)
		for c in css_checks:
			c.line = c.line or line
		checks += css_checks
		links += [u for u in css_links if u not in links]
	if not allow_scripts and any(c.level == "critical" and c.code in UNSAFE_HTML for c in checks):
		checks.append(Check("html.allow_scripts", "info",
			_("Only if the user explicitly wants active content: send allow_scripts: true (the change becomes high risk)")))
	return checks, links


CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.I)


def scan_css(css, allow_scripts=False, where=""):
	css = cstr(css)
	checks, links = [], []
	if re.search(r"expression\s*\(|javascript:|behavior\s*:|-moz-binding", css, re.I):
		checks.append(Check("css.script", "warning" if allow_scripts else "critical",
			_("{0}: the CSS contains script (expression(), javascript:, behavior)").format(where)))
	for m in re.finditer(r"@import\s+(?:url\()?\s*['\"]?([^'\";)]+)", css, re.I):
		checks.append(Check("css.import", "warning", _("{0}: @import loads another stylesheet: {1}").format(where, m.group(1))))
		if _is_external(m.group(1)):
			links.append(m.group(1))
	for m in CSS_URL_RE.finditer(css):
		if _bad_scheme(m.group(1)):
			checks.append(Check("css.script", "warning" if allow_scripts else "critical",
				_("{0}: the CSS loads a javascript:/data: address").format(where)))
		elif _is_external(m.group(1)) and m.group(1) not in links:
			links.append(m.group(1))
	return checks, links


def _dotted(node):
	from jinja2 import nodes

	parts = []
	while isinstance(node, nodes.Getattr | nodes.Getitem):
		if isinstance(node, nodes.Getattr):
			parts.append(node.attr)
		elif isinstance(node.arg, nodes.Const):
			parts.append(cstr(node.arg.value))
		else:
			parts.append("?")
		node = node.node
	if isinstance(node, nodes.Name):
		parts.append(node.name)
		return ".".join(reversed(parts))
	return None


def is_jinja(main_section, dynamic_template):
	"""Frappe's own rule (web_page.py::render_dynamic)."""
	main_section = cstr(main_section)
	return bool(dynamic_template) or "<!-- jinja -->" in main_section or "{{" in main_section


def scan_jinja(template):
	"""Syntax, and no data access or request echo in a template rendered for anonymous visitors."""
	from frappe.utils.jinja import get_jenv
	from jinja2 import TemplateSyntaxError, nodes

	template = cstr(template)
	if ".__" in template:  # frappe.utils.jinja.render_template refuses these
		return [Check("jinja.illegal", "critical", _("The template contains '.__', which Frappe refuses to render"))]
	env = get_jenv()
	try:
		tree = env.parse(template)
	except TemplateSyntaxError as e:
		return [Check("jinja.syntax", "critical", _("Jinja syntax error: {0}").format(e.message), e.lineno)]
	risky_globals = {n for n in env.globals if not n.startswith("_")} - SAFE_JINJA_NAMES - {"frappe"}
	checks, seen = [], set()
	chain_bases = set()
	for node in tree.find_all((nodes.Getattr, nodes.Getitem)):
		base = node
		while isinstance(base, nodes.Getattr | nodes.Getitem):
			base = base.node
		chain_bases.add(id(base))

	def flag(bad, node, message):
		if bad not in seen:
			seen.add(bad)
			checks.append(Check("jinja.data", "critical", message, getattr(node, "lineno", None)))

	data_message = _("{0} in the template runs for every anonymous visitor (reads data, calls the server or echoes the request); "
		"only formatting (frappe.utils, frappe.format_value, _) is allowed on a public page")
	for node in tree.find_all(nodes.Name):
		if node.name == "frappe" and id(node) not in chain_bases:
			flag("frappe", node, _("frappe is passed around as a value in the template; use frappe.utils / frappe.format_value directly"))
	for node in tree.find_all(nodes.Call):
		dotted = _dotted(node.node) or ""
		if dotted.startswith("doc.") and dotted.rsplit(".", 1)[-1] not in ("get", "get_formatted"):
			flag(dotted, node, _("{0}() calls a method of the page document from the template").format(dotted))
	attr_bases = {id(n.node) for n in tree.find_all((nodes.Getattr, nodes.Getitem))}
	for node in tree.find_all((nodes.Getattr, nodes.Getitem, nodes.Name)):
		dotted = _dotted(node) if not isinstance(node, nodes.Name) else node.name
		if not dotted:
			continue
		root, _sep, rest = dotted.partition(".")
		bad = None
		if root == "frappe":
			attr, _sep2, sub = rest.partition(".")
			if not attr:
				continue  # the bare Name node of a frappe.x chain; the chain itself is checked
			if attr not in SAFE_FRAPPE_ATTRS:
				bad = "frappe." + attr
			elif attr == "utils" and sub and sub.split(".", 1)[0] not in SAFE_UTILS:
				# frappe.utils also holds file and request readers (pdf_to_base64 returns a private file's bytes)
				bad = "frappe.utils." + sub.split(".", 1)[0]
			elif attr == "utils" and not sub and isinstance(node, nodes.Getattr) and id(node) not in attr_bases:
				bad = "frappe.utils"   # utils passed around whole
		elif root in risky_globals and isinstance(node, nodes.Name):
			bad = root
		if bad:
			flag(bad, node, data_message.format(bad))
	if not checks:
		checks.append(Check("jinja.syntax", "ok", _("The Jinja template parses and only formats values")))
	return checks


# -- dry-run renderer ------------------------------------------------------------------------------


@contextlib.contextmanager
def _render_state(path, form_dict=None):
	local = frappe.local
	saved = {"form_dict": getattr(local, "form_dict", None), "path": getattr(local, "path", None),
		"redirect": local.flags.get("redirect_location"), "no_cache": getattr(local, "no_cache", None)}
	local.form_dict = frappe._dict(form_dict or {})
	local.path = path
	try:
		yield
	finally:
		local.form_dict = saved["form_dict"] if saved["form_dict"] is not None else frappe._dict()
		local.path = saved["path"]
		local.flags.redirect_location = saved["redirect"]
		if saved["no_cache"] is None:
			with contextlib.suppress(AttributeError):
				del local.no_cache
		else:
			local.no_cache = saved["no_cache"]


def render_page(doc, path, user, form_dict=None):
	"""Full HTML of an unsaved Web Page / Web Form as ``user`` would get it, inside a savepoint."""
	from frappe.website.page_renderers.document_page import DocumentPage

	class PreviewPage(DocumentPage):
		def html(self):
			self.doc = doc
			self.doctype, self.docname = doc.doctype, doc.name
			self.init_context()
			self.update_context()
			self.post_process_context()
			return frappe.get_template(self.template_path).render(self.context)

	savepoint = "ai_web_preview"
	frappe.db.savepoint(savepoint)
	try:
		with _base.as_user(user), _render_state(path, form_dict):
			return PreviewPage(path).html()
	finally:
		frappe.db.rollback(save_point=savepoint)


def _excerpt(html, markers):
	html = cstr(html)
	start = 0
	for marker in markers:
		pos = html.find(marker)
		if pos >= 0:
			start = pos
			break
	end = html.find("<footer", start)
	end = end if end > start else len(html)
	return html[start:min(end, start + PREVIEW_CHARS)].strip()


# -- the kind ------------------------------------------------------------------------------------------
