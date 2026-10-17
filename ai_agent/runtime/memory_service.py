import frappe
import json
from frappe.utils import cint

from ai_agent.runtime.constants import ARABIC_NORMALIZATION_MAP


MAX_MEMORY_CONTEXT_CHARS = 4000
MAX_MEMORY_CONTENT_CHARS = 1200


def normalize_entity_key(value):
    """Canonical Arabic-folded, whitespace-collapsed, lowercased entity name."""
    text = " ".join(str(value or "").split()).lower()
    for source, target in ARABIC_NORMALIZATION_MAP:
        text = text.replace(source, target)
    return text


class MemoryService:
    """Manages agent memory (disabled by default in MVP)."""

    def is_enabled(self):
        settings = frappe.get_cached_doc("AI Settings")
        return settings.get("enable_memory", 0)

    def store(
        self, user, scope, memory_type, content, company=None, agent=None,
        sensitivity="Low", source_run=None, entity_type=None, entity_key=None,
        mapping=None,
    ):
        """Store a memory entry."""
        if not self.is_enabled():
            return None

        content = self._clean(content, MAX_MEMORY_CONTENT_CHARS)
        if not content:
            return None

        entity_key = normalize_entity_key(entity_key) if entity_key else None
        mapping_json = (
            json.dumps(mapping, ensure_ascii=False, sort_keys=True, default=str)
            if isinstance(mapping, dict) else None
        )

        # One memory per topic: a memory naming an entity (a supplier's mapping, "Journal
        # Entry rules"…) replaces the previous one instead of piling up near-duplicates.
        if entity_type and entity_key:
            filters = {
                "memory_type": memory_type,
                "scope": scope,
                "entity_type": entity_type,
                "entity_key": entity_key,
            }
            if scope == "Agent" and agent:
                # Agent-scope mappings key on (agent, entity); company is
                # informational there and may be auto-filled from defaults.
                filters["agent"] = agent
            elif scope == "Company":
                filters["company"] = company
        else:
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
            if source_run or (entity_type and entity_key):
                memory.update({
                    "user": user,
                    "agent": agent if scope == "Agent" else None,
                    "company": (company or "") if scope in ("Agent", "Company") else None,
                    "scope": scope,
                    "content": content,
                    "sensitivity": sensitivity,
                    "entity_type": entity_type or memory.get("entity_type"),
                    "entity_key": entity_key or memory.get("entity_key"),
                    "mapping_json": mapping_json or memory.get("mapping_json"),
                    "usage_count": cint(memory.get("usage_count")) + 1,
                    "source_run": source_run or memory.get("source_run"),
                })
                memory.save(ignore_permissions=True)
                frappe.db.commit()
            return memory

        memory = frappe.get_doc({
            "doctype": "AI Memory",
            "user": user,
            "agent": agent if scope == "Agent" else None,
            "company": (company or "") if scope in ("Agent", "Company") else None,
            "source_run": source_run,
            "scope": scope,
            "memory_type": memory_type,
            "content": content,
            "sensitivity": sensitivity,
            "entity_type": entity_type,
            "entity_key": entity_key,
            "mapping_json": mapping_json,
            "usage_count": 1,
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

    def build_context(self, user, agent=None, company=None, limit=8, query_text=""):
        """Build compact long-term memory context, most relevant to the
        current prompt first (entity match, then token overlap, then recency)."""
        memories = []
        for scope in ("User", "Agent", "Company"):
            memories.extend(self.retrieve(
                user=user,
                scope=scope,
                company=company,
                agent=agent,
            ))

        # retrieve() already scopes by the structured user/agent/company fields,
        # so no re-filtering on content substrings is needed here.
        if not memories:
            return ""

        normalized_query = normalize_entity_key(query_text)
        memories.sort(
            key=lambda row: self._relevance_key(row, normalized_query),
            reverse=True,
        )
        lines = ["Relevant learned context from previous runs. Apply only when it matches the current task:"]
        for memory in memories[:limit]:
            line = f"- {self._clean(memory.content, MAX_MEMORY_CONTENT_CHARS)}"
            if memory.get("mapping_json"):
                line += f" | mapping: {self._clean(memory.get('mapping_json'), 300)}"
            lines.append(line)

        text = "\n".join(lines)
        if len(text) > MAX_MEMORY_CONTEXT_CHARS:
            text = text[:MAX_MEMORY_CONTEXT_CHARS].rstrip() + "\n... [memory context truncated]"
        return text

    def _relevance_key(self, row, normalized_query):
        score = 0
        if normalized_query:
            entity_key = row.get("entity_key") or ""
            if entity_key and entity_key in normalized_query:
                score += 6
            query_tokens = {t for t in normalized_query.split() if len(t) >= 3}
            if query_tokens:
                content_tokens = set(normalize_entity_key(row.content or "").split())
                score += len(content_tokens & query_tokens)
        if row.memory_type == "Mapping":
            score += 1
        return (score, str(row.modified or ""))

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
        except (TypeError, ValueError):
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
