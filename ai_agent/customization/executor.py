"""Execute an approved Customize request, as the approver (docs/customization/01 §3.2 "execution path").

1. The target must still be what the requester saw: ``kind.snapshot(spec)`` equals ``payload.before``,
   otherwise ConcurrencyConflict ("the target changed since the request").
2. The full check runs again for the approver; any critical check stops it.
3. ``kind.execute`` under the approver's session, then ``kind.verify``.
4. ``execution_result`` = {kind, operation, targets, undo, after, verified, checks, report}. A change that
   executed but did not verify is still Executed, with ``verified: false`` (the card says so).
"""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager

import frappe

from ai_agent.customization import registry
from ai_agent.customization.contract import Check, CheckReport
from ai_agent.runtime.exceptions import ConcurrencyConflict, PermissionDenied


def canonical(value):
	"""Stable JSON for comparing snapshots (key order and types like dates do not matter)."""
	return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)


def same_state(a, b):
	return canonical(a) == canonical(b)


@contextmanager
def as_user(user):
	"""The decision runs in the approver's own request; switch only when called from elsewhere."""
	previous = frappe.session.user
	if previous == user:
		yield
		return
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


def _report(value):
	if isinstance(value, CheckReport):
		return value
	if isinstance(value, dict):
		return CheckReport.from_dict(value)
	return CheckReport()


def _checks(values):
	out = []
	for value in values or []:
		out.append(value if isinstance(value, Check) else Check.from_dict(value if isinstance(value, dict) else {"code": "verify", "level": "info", "message": str(value)}))
	return out


def critical_message(report):
	lines = []
	for check in report.critical():
		where = f" (line {check.line})" if check.line else ""
		lines.append(f"{check.message}{where}")
	return "; ".join(lines)


def _execute(kind, spec, user, approval_name):
	"""The approval name goes into the provenance header of scripts and reports (kinds read the argument, or
	``frappe.flags.ai_customize_approval``)."""
	previous = frappe.flags.get("ai_customize_approval")
	frappe.flags.ai_customize_approval = approval_name
	try:
		try:
			accepts = "approval" in inspect.signature(kind.execute).parameters
		except (TypeError, ValueError):
			accepts = False
		return kind.execute(spec, user, approval=approval_name) if accepts else kind.execute(spec, user)
	finally:
		frappe.flags.ai_customize_approval = previous


def run(approval, user):
	payload = json.loads(approval.payload_json or "{}")
	kind_name = payload.get("kind")
	spec = payload.get("spec") or {}
	kind = registry.get(kind_name)

	with as_user(user):
		current = kind.snapshot(spec)
		if not same_state(current, payload.get("before")):
			raise ConcurrencyConflict("The target changed since this was requested; ask for the change again.")

		report = _report(kind.check(spec, user))
		if report.critical():
			raise PermissionDenied(f"The change no longer passes its checks: {critical_message(report)}")

		try:
			result = _execute(kind, spec, user, approval.name) or {}
		except Exception as exc:
			if getattr(exc, "created_before_compensation", None) is not None:
				# The kind created records (their DDL committed them) and deleted them again; keep those deletions:
				# the engine rolls back after a failure, which would bring the deleted rows back.
				frappe.db.commit()
			raise
		checks = _checks(kind.verify(spec, result))
		try:
			after = kind.snapshot(spec)
		except Exception:
			# the change is applied; without "after" the undo cannot compare the target, so leave a trace
			frappe.log_error(frappe.get_traceback(), f"ai_agent customize: {kind_name} snapshot after execute failed")
			after = None

	verified = not any(c.level == "critical" for c in checks)
	execution = {
		"kind": kind_name,
		"operation": payload.get("operation") or spec.get("operation"),
		"targets": result.get("targets") or [],
		"undo": result.get("undo"),
		"after": after,
		"verified": verified,
		"checks": [c.to_dict() for c in checks],
		"report": report.to_dict(),
	}
	for key, value in result.items():
		execution.setdefault(key, value)  # kind-specific extras (partial, created_before_compensation...)

	approval.status = "Executed"
	approval.executed_at = frappe.utils.now_datetime()
	approval.execution_result = canonical(execution)
	approval.save(ignore_permissions=True)
	frappe.db.commit()
	return execution
