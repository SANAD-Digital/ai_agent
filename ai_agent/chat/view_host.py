"""Desk as an MCP Apps host.

The Desk chat renders tool results with the very same MCP App view that
ChatGPT / Claude load (mcp/ui). This module produces the tool-result object
the view expects and proxies the calls the view makes back (paging, sorting,
search, drill-down, full-view loading) through the MCP service, so both
surfaces share one presentation layer and one set of rules.
"""

from __future__ import annotations

import hashlib

import frappe

from ai_agent.presentation import present

VIEW_TYPES = {"table", "document", "cards", "choice"}      # approvals/errors have native chat cards
VIEW_TOOLS_ALLOWED_PREFIXES = ("view__",)
MAX_VIEW_BYTES = 400_000


def service_for(agent_doc, company=None, user=None):
    from ai_agent.mcp.service import MCPService

    service = MCPService(user or frappe.session.user)
    service.agent = agent_doc                 # the chat session's agent, not the MCP connector's
    service.company = company
    service.ui_supported = True
    return service


def view_result(tool, arguments, result, ctx, agent_doc):
    """Full MCP tool-result for the view, or None when the tool has no view."""
    envelope = present(tool, arguments, result, ctx)
    if not envelope or not isinstance(envelope.get("presentation"), dict):
        return None
    if envelope["presentation"].get("type") not in VIEW_TYPES:
        return None
    service = service_for(agent_doc, getattr(ctx, "company", None), getattr(ctx, "user", None))
    out = service._envelope_result(envelope, tool, arguments, for_app=True)
    size = len(frappe.as_json(out))
    if size > MAX_VIEW_BYTES:
        return None
    return out


def view_html():
    from ai_agent.mcp.protocol import build_main_view

    html = build_main_view()
    return html, hashlib.sha256(html.encode()).hexdigest()[:16]


def call(agent_doc, company, name, arguments):
    """A tools/call made by the view. Only app tools and read-only tools pass:
    writes go through the chat's own approval cards, never through the view."""
    from ai_agent.mcp.service import SAFE_SIDE_EFFECTS

    service = service_for(agent_doc, company)
    if not name.startswith(VIEW_TOOLS_ALLOWED_PREFIXES):
        registered = service.registry.get_tool(service._internal_name(name))
        if not registered or (registered.get("side_effect") or "read_only") not in SAFE_SIDE_EFFECTS or not service._is_exposed(registered):
            frappe.throw(frappe._("This action is not available from the chat view."), frappe.PermissionError)
    return service.call_tool(name, dict(arguments or {}))
