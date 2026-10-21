"""The frozen contract between the customization runtime (group A) and the kinds/analyzers (group B)
and the card (group C). docs/customization/01-gap-audit-and-design.md §3.2 and §3.4.

A kind lives in ``ai_agent/customization/kinds/<name>.py`` and exposes a module attribute ``KIND``:
an instance (or a class with a no-argument constructor) that implements ``Kind`` below.
``registry.KINDS`` maps the kind name to that dotted path.

Approval payload (``AI Approval Request.payload_json`` for ``action_type="Customize"``)::

    {
      "kind": "server_script",            # registry name
      "operation": "create",              # spec["operation"] (properties: "update")
      "spec": {...},                      # kind.normalize(args, user)
      "before": {...},                    # kind.snapshot(spec) at request time (compared again at execution)
      "report": CheckReport.to_dict(),    # kind.check(spec, requester)
      "summary": "...",                   # kind.summary(spec)
      "target": "server_script:My Script",# policy.target_key(kind, spec): one pending request per target
      "purpose": "...",                   # args["purpose"] or args["reason"], shown on the card
      "context": {"surface": "Desk"|"MCP", "tainted": bool, "via": tool name, "requested_by": user},
      "undo_of": "<approval name>",       # only on a customize.request_undo request
    }

Execution result (``AI Approval Request.execution_result``)::

    {"kind", "operation", "targets": [{"doctype", "name"}], "undo": {...spec of the same kind},
     "after": kind.snapshot(spec) right after execution, "verified": bool, "checks": [Check.to_dict()],
     "report": CheckReport.to_dict() (the re-check at execution)}
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

import frappe

LEVELS = ("ok", "info", "warning", "critical")
RISKS = ("low", "medium", "high")


@dataclass
class Check:
	code: str  # "py.import", "sql.table_permission", "field.name_conflict"...
	level: str  # "ok" | "info" | "warning" | "critical"
	message: str  # in the user's language (translated with _())
	line: int | None = None

	def to_dict(self):
		return asdict(self)

	@classmethod
	def from_dict(cls, data):
		data = data or {}
		return cls(code=str(data.get("code") or ""), level=str(data.get("level") or "info"),
			message=str(data.get("message") or ""), line=data.get("line"))


@dataclass
class CheckReport:
	checks: list[Check] = field(default_factory=list)
	risk: str = "low"  # "low" | "medium" | "high"
	impact: list[str] = field(default_factory=list)  # "Runs for everyone who opens a Sales Invoice"
	preview: dict = field(default_factory=dict)  # kind-specific: diff / code / rows / layout

	def critical(self):
		return [c for c in self.checks if c.level == "critical"]

	def to_dict(self):
		return {
			"checks": [c.to_dict() for c in self.checks],
			"risk": self.risk,
			"impact": list(self.impact or []),
			"preview": self.preview or {},
		}

	@classmethod
	def from_dict(cls, data):
		data = data or {}
		return cls(
			checks=[Check.from_dict(c) for c in data.get("checks") or []],
			risk=data.get("risk") or "low",
			impact=list(data.get("impact") or []),
			preview=data.get("preview") or {},
		)


class CustomizationError(frappe.ValidationError):
	"""Arguments the kind cannot turn into a spec. ``hints`` tell the model how to fix them."""

	def __init__(self, message, hints=None):
		super().__init__(message)
		self.message = message
		self.hints = list(hints or [])


@runtime_checkable
class Kind(Protocol):
	name: str  # "custom_field" | "properties" | "client_script" | "server_script" | "report"
	setting: str  # AI Settings check field that enables it (besides allow_customization_actions)
	required_roles: tuple  # every role is required: ("System Manager",) or ("System Manager", "Script Manager")

	def normalize(self, args: dict, user: str) -> dict: ...  # spec, or raise CustomizationError(message, hints)

	def snapshot(self, spec: dict) -> dict: ...  # comparable JSON "before" state of the target

	def check(self, spec: dict, user: str) -> CheckReport: ...  # never writes anything permanent

	def execute(self, spec: dict, user: str) -> dict: ...  # {"targets":[{doctype,name}], "undo": {...reverse spec}}

	def verify(self, spec: dict, result: dict) -> list[Check]: ...

	def summary(self, spec: dict) -> str: ...  # action_summary


def required_roles_for(kind, spec=None):
	"""A kind may need more roles for some specs (a Query Report needs Script Manager, a Builder one
	does not): it can expose ``roles_for(spec)``; otherwise ``required_roles``."""
	getter = getattr(kind, "roles_for", None)
	if callable(getter) and spec is not None:
		return tuple(getter(spec) or ())
	return tuple(getattr(kind, "required_roles", ()) or ())
