"""Kind name -> implementation, loaded lazily (a kind module may import heavy analyzers)."""

from __future__ import annotations

import importlib

from ai_agent.customization.contract import CustomizationError

KINDS = {
	"custom_field": "ai_agent.customization.kinds.custom_field.KIND",
	"properties": "ai_agent.customization.kinds.properties.KIND",
	"client_script": "ai_agent.customization.kinds.client_script.KIND",
	"server_script": "ai_agent.customization.kinds.server_script.KIND",
	"report": "ai_agent.customization.kinds.report.KIND",
}

# AI Settings check that enables each kind (besides the master switch allow_customization_actions).
KIND_SETTINGS = {
	"custom_field": "allow_customize_fields",
	"properties": "allow_customize_fields",
	"client_script": "allow_customize_client_scripts",
	"server_script": "allow_customize_server_scripts",
	"report": "allow_customize_reports",
}

# Frappe's own roles for each kind (D3: no new role). A kind's ``required_roles`` / ``roles_for(spec)``
# wins when it is loaded; these are used before it is (tool visibility) and as a floor.
KIND_ROLES = {
	"custom_field": ("System Manager",),
	"properties": ("System Manager",),
	"client_script": ("System Manager",),
	"server_script": ("System Manager", "Script Manager"),
	"report": ("System Manager",),
}

# customize.request_* tool -> kind
REQUEST_TOOLS = {
	"customize.request_field": "custom_field",
	"customize.request_properties": "properties",
	"customize.request_client_script": "client_script",
	"customize.request_server_script": "server_script",
	"customize.request_report": "report",
}
UNDO_TOOL = "customize.request_undo"
READ_TOOLS = ("customize.inspect", "customize.get", "customize.check")
ALL_TOOLS = READ_TOOLS + tuple(REQUEST_TOOLS) + (UNDO_TOOL,)

_overrides = {}  # tests register fake kinds here


def get(name):
	if name in _overrides:
		return _overrides[name]
	path = KINDS.get(name)
	if not path:
		raise CustomizationError(f"Unknown customization kind '{name}'.", hints=[f"Kinds: {', '.join(KINDS)}"])
	module_path, attr = path.rsplit(".", 1)
	try:
		obj = getattr(importlib.import_module(module_path), attr)
	except (ImportError, AttributeError):
		raise CustomizationError(f"Customization kind '{name}' is not installed on this site.")
	return obj() if isinstance(obj, type) else obj


def register(name, kind):
	"""Test hook: serve ``kind`` for ``name`` (a fake kind needs no database)."""
	_overrides[name] = kind


def unregister(name):
	_overrides.pop(name, None)
