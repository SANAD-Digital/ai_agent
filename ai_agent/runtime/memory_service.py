import frappe
import json
from frappe import _
from frappe.utils import cint


MAX_MEMORY_CONTEXT_CHARS = 4000
MAX_MEMORY_CONTENT_CHARS = 1200


class MemoryService:
    """Manages agent memory (disabled by default in MVP)."""

    def is_enabled(self):
        settings = frappe.get_doc("AI Settings")
        return settings.get("enable_memory", 0)

    def store(
        self, user, scope, memory_type, content, company=None, agent=None,
        sensitivity="Low", source_run=None,
    ):
        """Store a memory entry."""
        if not self.is_enabled():
            return None

        content = self._clean(content, MAX_MEMORY_CONTENT_CHARS)
        if not content:
            return None

        filters = {
            "scope": scope,
            "memory_type": memory_type,
            "content": content,
            "user": user,
            "agent": agent if scope == "Agent" else None,
            "company": company if scope in ("Agent", "Company") else None,
        }
        if source_run:
            filters = {"source_run": source_run, "memory_type": memory_type}
        existing = frappe.db.exists("AI Memory", filters)
        if existing:
            memory = frappe.get_doc("AI Memory", existing)
            if source_run:
                memory.update({
                    "user": user,
                    "agent": agent if scope == "Agent" else None,
                    "company": company if scope in ("Agent", "Company") else None,
                    "scope": scope,
                    "content": content,
                    "sensitivity": sensitivity,
                })
                memory.save(ignore_permissions=True)
                frappe.db.commit()
            return memory

        memory = frappe.get_doc({
            "doctype": "AI Memory",
            "user": user,
            "agent": agent if scope == "Agent" else None,
            "company": company if scope in ("Agent", "Company") else None,
            "source_run": source_run,
            "scope": scope,
            "memory_type": memory_type,
            "content": content,
            "sensitivity": sensitivity,
        })
        memory.insert(ignore_permissions=True)
        frappe.db.commit()
        return memory

    def retrieve(self, user, scope="User", memory_type=None, company=None, agent=None):
        """Retrieve memories for a user."""
        if not self.is_enabled():
            return []

        filters = {"scope": scope}
        if scope == "User":
            filters["user"] = user
        elif scope == "Agent":
            if not agent:
                return []
            filters["agent"] = agent
            filters["user"] = user
            if company:
                filters["company"] = ("in", [company, ""])
        elif scope == "Company":
            if not company:
                return []
            filters["company"] = company
        if memory_type:
            filters["memory_type"] = memory_type

        memories = frappe.get_all("AI Memory", filters=filters, fields=["*"])

        valid = []
        now = frappe.utils.now_datetime()
        for m in memories:
            if m.expires_at and m.expires_at < now:
                continue
            valid.append(m)

        return valid

    def build_context(self, user, agent=None, company=None, limit=8):
        """Build compact long-term memory context for model prompts."""
        memories = []
        for scope in ("User", "Agent", "Company"):
            for memory_type in ("Preference", "Summary", "Context"):
                memories.extend(self.retrieve(
                    user=user,
                    scope=scope,
                    memory_type=memory_type,
                    company=company,
                    agent=agent,
                ))

        filtered = []
        for memory in memories:
            content = memory.content or ""
            if agent and f"agent={agent}" in content:
                filtered.append(memory)
            elif company and f"company={company}" in content:
                filtered.append(memory)
            elif memory.scope == "User":
                filtered.append(memory)

        if not filtered:
            return ""

        filtered.sort(key=lambda row: row.modified, reverse=True)
        lines = ["Relevant learned context from previous runs. Apply only when it matches the current task:"]
        for memory in filtered[:limit]:
            lines.append(f"- {self._clean(memory.content, MAX_MEMORY_CONTENT_CHARS)}")

        text = "\n".join(lines)
        if len(text) > MAX_MEMORY_CONTEXT_CHARS:
            text = text[:MAX_MEMORY_CONTEXT_CHARS].rstrip() + "\n... [memory context truncated]"
        return text

    def store_run_lesson(self, run_doc, lesson, memory_type="Context", sensitivity="Medium"):
        """Store a compact lesson from a run when memory is enabled."""
        if not lesson or not self.is_enabled():
            return None

        content = (
            f"agent={run_doc.agent}; company={run_doc.company or ''}; "
            f"status={run_doc.status}; lesson={self._clean(lesson, MAX_MEMORY_CONTENT_CHARS)}"
        )
        return self.store(
            user=run_doc.user,
            scope="Agent",
            memory_type=memory_type,
            content=content,
            company=run_doc.company,
            agent=run_doc.agent,
            sensitivity=sensitivity,
            source_run=run_doc.name,
        )

    def store_completed_run(self, run_doc, final_answer=None):
        """Persist a bounded, searchable episode from a successful tool-backed run."""
        if run_doc.status != "Completed" or not cint(run_doc.tool_call_count):
            return None

        answer = final_answer or run_doc.final_answer
        if not self._is_reusable_outcome(answer):
            return None

        objective = self._clean(run_doc.prompt, 320)
        outcome = self._clean(answer, 800)
        if not outcome:
            return None
        return self.store_run_lesson(
            run_doc,
            f"Successful run. Objective: {objective}. Previous outcome; verify against current ERP evidence before reuse: {outcome}",
            memory_type="Summary",
            sensitivity="Medium",
        )

    def _is_reusable_outcome(self, answer):
        text = str(answer or "").strip()
        if not text:
            return False
        try:
            payload = json.loads(text)
        except Exception:
            lowered = text.lower()
            return not any(marker in lowered for marker in ("needs review", "failed", "تعذر", "لم أتمكن"))
        if not isinstance(payload, dict):
            return False
        if str(payload.get("status") or "").lower() in {"failed", "needs review", "error"}:
            return False
        if payload.get("errors"):
            return False
        return True

    def _clean(self, text, limit):
        value = " ".join(str(text or "").split())
        if len(value) > limit:
            return value[:limit].rstrip() + "…"
        return value

    def clear_expired(self):
        """Remove expired memories."""
        now = frappe.utils.now_datetime()
        expired = frappe.get_all("AI Memory", filters={"expires_at": ("<", now)}, pluck="name")
        for name in expired:
            frappe.delete_doc("AI Memory", name, ignore_permissions=True)
        self.clear_unusable_run_memories()
        frappe.db.commit()

    def clear_unusable_run_memories(self):
        """Remove generated episodes whose source run has no reusable outcome."""
        rows = frappe.get_all(
            "AI Memory",
            filters={"source_run": ("is", "set")},
            fields=["name", "source_run"],
        )
        for row in rows:
            run = frappe.db.get_value(
                "AI Run",
                row.source_run,
                ["status", "final_answer"],
                as_dict=True,
            )
            if not run or run.status != "Completed" or not self._is_reusable_outcome(run.final_answer):
                frappe.delete_doc("AI Memory", row.name, ignore_permissions=True)


def clear_expired():
    """Scheduled task to clear expired memories."""
    service = MemoryService()
    return service.clear_expired()


def backfill_completed_run(run_name):
    """Create the memory episode for one existing completed run, idempotently."""
    run_doc = frappe.get_doc("AI Run", run_name)
    memory = MemoryService().store_completed_run(run_doc)
    return memory.name if memory else None
