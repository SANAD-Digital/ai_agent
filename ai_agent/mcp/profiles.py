"""Which AI MCP record a request uses: the user's record for its connection (the OAuth client of the bearer
token) first, then the user's record without a connection. A disabled record refuses MCP; no record leaves the
site-wide agent and company (AI Settings) unless the site requires a record per user.

What a connection may do is on its record (DEFAULTS, read through ``flag`` and ``setting``): whether it may ask
for changes, whether they wait for a person, the card's buttons, customization requests and the request rate. A
user without a record has the defaults: they read, and nothing changes without a person.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, now_datetime

CACHE_KEY = "ai_agent:mcp_profiles"
TOUCH_KEY = "ai_agent:mcp_profile_used:{}"
TOUCH_SECONDS = 600  # last_used_on is a hint for the admin: written at most every 10 minutes
ATTR = "ai_agent_mcp_record"
# Unset (never saved) is the default too, so approval is only ever switched off on purpose.
DEFAULTS = {
	"allow_action_requests": 0,
	"require_approval": 1,
	"approval_buttons": 1,
	"allow_customization": 0,
	"max_requests_per_minute": 60,
}


def records(user):
	"""Every AI MCP record of ``user`` (enabled or not), cached per user until a record changes."""
	cached = frappe.cache.hget(CACHE_KEY, user)
	if cached is not None:
		return [frappe._dict(row) for row in cached]
	rows = frappe.get_all("AI MCP", filters={"user": user}, fields=["name", "title", "agent", "company", "connection", "enabled", *DEFAULTS])
	frappe.cache.hset(CACHE_KEY, user, [dict(row) for row in rows])
	return rows


def resolve(user, connection=None):
	"""The record that applies, or None when the user has none for this connection or in general."""
	rows = records(user)
	if connection:
		for row in rows:
			if row.connection == connection:
				return row
	return next((row for row in rows if not row.connection), None)


def use(record):
	"""The record this request works under, None without one (MCPService sets it; frappe.local: one request)."""
	setattr(frappe.local, ATTR, record)


def setting(name):
	value = (getattr(frappe.local, ATTR, None) or {}).get(name)
	return DEFAULTS[name] if value is None or value == "" else value


def flag(name):
	return bool(cint(setting(name)))


def touch(name):
	if not name or frappe.cache.get_value(TOUCH_KEY.format(name)):
		return
	frappe.cache.set_value(TOUCH_KEY.format(name), 1, expires_in_sec=TOUCH_SECONDS)
	frappe.db.set_value("AI MCP", name, "last_used_on", now_datetime(), update_modified=False)


def clear_cache(*args, **kwargs):
	frappe.cache.delete_value(CACHE_KEY)
