import frappe
from frappe import _


MAX_MEMORY_CONTEXT_CHARS = 4000
MAX_MEMORY_CONTENT_CHARS = 1200


class MemoryService:
    """Manages agent memory (disabled by default in MVP)."""

    def is_enabled(self):
        settings = frappe.get_doc("AI Settings")
        return settings.get("enable_memory", 0)

    def store(self, user, scope, memory_type, content, company=None, agent=None, sensitivity="Low"):
        """Store a memory entry."""
        if not self.is_enabled():
            return None

        memory = frappe.get_doc({
            "doctype": "AI Memory",
            "user": user,
            "agent": agent if scope == "Agent" else None,
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
            memories.extend(self.retrieve(user=user, scope=scope, memory_type="Summary", company=company, agent=agent))
            memories.extend(self.retrieve(user=user, scope=scope, memory_type="Context", company=company, agent=agent))

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
        )

    def _clean(self, text, limit):
        value = " ".join(str(text or "").split())
        if len(value) > limit:
            return value[:limit].rstrip() + "…"
        return value

    def clear_expired(self):
        """Remove expired memories."""
        now = frappe.utils.now_datetime()
        expired = frappe.get_all("AI Memory", filters={"expires_at": (" <", now)}, pluck="name")
        for name in expired:
            frappe.delete_doc("AI Memory", name, ignore_permissions=True)
        frappe.db.commit()


def clear_expired():
    """Scheduled task to clear expired memories."""
    service = MemoryService()
    return service.clear_expired()
