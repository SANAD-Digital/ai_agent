"""Maps a tool result to its presenter; the generic presenter is only the fallback."""

import frappe

from ai_agent.presentation import schema

_PRESENTERS = {}


def register(*tool_names):
    def wrap(fn):
        for name in tool_names:
            _PRESENTERS[name] = fn
        return fn

    return wrap


def _load():
    # Importing the package registers every presenter.
    from ai_agent.presentation import presenters  # noqa: F401


def present(tool, arguments, result, ctx=None):
    """Return the standard envelope for a tool result, or None when the tool has no view.

    A presenter failure never breaks the tool call: the caller falls back to text.
    """
    _load()
    presenter = _PRESENTERS.get(tool)
    if not presenter or not isinstance(result, dict) or result.get("error"):
        return None
    try:
        return presenter(tool, arguments or {}, result, ctx)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Presenter failed: {tool}")
        return None


def registered_tools():
    _load()
    return sorted(_PRESENTERS)


__all__ = ["present", "register", "registered_tools", "schema"]
