"""Structured lifecycle events for the MCP server, the approval flow and the embedded view.

One JSON line per event in ``logs/ai_agent_mcp.jsonl`` (per site) and a short ring buffer for ``ai_agent.mcp.api.diagnostics``.
Only identifiers are logged: never payloads, tokens or tool arguments.
"""

import json
import os

import frappe

LOGGER = "ai_agent_mcp"
MAX_BYTES = 5_000_000

EVENTS = {
    # server / tools
    "tool_registered", "tool_called", "fallback_used", "host_seen", "request_received", "request_failed",
    # approval lifecycle
    "approval_created", "approval_loaded", "approval_decision_received", "approval_approved", "approval_rejected",
    "approval_execute_started", "approval_execute_completed", "approval_verify_started", "approval_verified", "approval_failed",
    "approval_skipped", "approval_direct_execute", "continuation_stored", "continuation_started", "continuation_completed", "continuation_failed",
    # UI resource and view
    "ui_resource_requested", "ui_resource_loaded", "ui_resource_failed",
    "ui_handshake_started", "ui_handshake_completed", "ui_tool_result_received", "ui_render_started", "ui_render_completed", "ui_render_failed",
}
VIEW_EVENTS = {e for e in EVENTS if e.startswith("ui_") and e not in ("ui_resource_requested", "ui_resource_loaded", "ui_resource_failed")}


def request_id():
    if not getattr(frappe.local, "mcp_request_id", None):
        frappe.local.mcp_request_id = frappe.generate_hash(length=10)
    return frappe.local.mcp_request_id


def host_name():
    try:
        return (frappe.request.headers.get("User-Agent") or "")[:60] if getattr(frappe, "request", None) else None
    except Exception:
        return None


def log(event, *, user=None, tool=None, approval_id=None, doctype=None, **extra):
    """Record one event. Never raises: logging must not break a request."""
    if frappe.flags.in_test:
        return   # automated tests must not write into the real host log
    try:
        entry = {"ts": str(frappe.utils.now_datetime()), "event": event, "request_id": request_id(), "user": user or frappe.session.user,
                 "tool": tool, "approval_id": approval_id, "doctype": doctype, "host": host_name(),
                 "ip": getattr(frappe.local, "request_ip", None), "client": oauth_client()}
        entry.update({k: v for k, v in extra.items() if v is not None and k not in ("token", "payload", "arguments", "secret")})
        line = (json.dumps(entry, ensure_ascii=False, default=str)[:3800] + "\n").encode("utf-8")
        path = log_path()
        # O_APPEND writes of one short line are atomic, so nine workers can share the file without a lock or a
        # rotating handler (which loses lines when several processes rotate at once).
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line)
            size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if size > MAX_BYTES:
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
    except Exception:
        pass


def oauth_client():
    """The OAuth client (connector) that sent this request, from its bearer token; None for a Desk session."""
    try:
        header = (frappe.get_request_header("Authorization", "") or "").split(" ")
        if len(header) == 2 and header[0].lower() == "bearer":
            return frappe.db.get_value("OAuth Bearer Token", header[1], "client")
    except Exception:
        pass
    return None


def log_path():
    return os.path.join(frappe.get_site_path("logs"), f"{LOGGER}.jsonl")


def recent(limit=100, event=None, user=None):
    """The last events from the site's log file (durable: survives cache clears and restarts)."""
    try:
        with open(log_path(), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 400_000))
            lines = handle.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        start = line.find("{")
        if start < 0:
            continue
        try:
            entry = json.loads(line[start:])
        except ValueError:
            continue
        if (not event or entry.get("event") == event) and (not user or entry.get("user") == user):
            out.append(entry)
    return out[-limit:]
