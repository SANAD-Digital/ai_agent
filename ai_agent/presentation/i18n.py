"""Wording for result views (chat cards and MCP Apps): English source text, shown in the viewer's language.

Strings are translated in the "AI View" context (translations/ar.csv), so a common word keeps
its own wording elsewhere in Desk. Translate at the point of use, never at import time: module
constants hold English and the viewer's language is only known during a request.
"""

import frappe

CONTEXT = "AI View"


def t(text, *args):
	"""`text` in the viewer's language; `{0}`-style placeholders are filled with `args`."""
	from ai_agent.presentation.labels import label_language

	try:
		out = frappe._(text, lang=label_language(), context=CONTEXT)
	except Exception:
		out = text
	return out.format(*args) if args else out


def view_strings(lang):
	"""Every "AI View" translation for `lang`, for the embedded view (which runs outside Frappe)."""
	if not lang or lang.startswith("en"):
		return {}
	from frappe.translate import get_all_translations

	suffix = ":" + CONTEXT
	return {key[: -len(suffix)]: value for key, value in get_all_translations(lang).items() if key.endswith(suffix)}


def is_rtl(lang):
	return str(lang or "").split("-")[0] in ("ar", "he", "fa", "ur")
