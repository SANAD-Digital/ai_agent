from frappe import _

from ai_agent.runtime.memory_service import MemoryService


def handle_search(arguments, permission_context):
    """Search compact AI memory entries for the current user/agent/company."""
    query = _clean(arguments.get("query"), 180).lower()
    if not query:
        return {"error": "query is required"}

    service = MemoryService()
    if not service.is_enabled():
        return {"rows": [], "row_count": 0, "message": _("AI memory is disabled.")}

    company = getattr(permission_context, "company", None) or arguments.get("company")
    agent = getattr(permission_context, "agent_name", None) or arguments.get("agent")
    limit = max(1, min(int(arguments.get("limit") or 8), 20))

    rows = []
    for scope in ("User", "Agent", "Company"):
        for memory_type in ("Preference", "Context", "Summary"):
            rows.extend(service.retrieve(
                user=permission_context.user,
                scope=scope,
                memory_type=memory_type,
                company=company,
                agent=agent,
            ))

    unique_rows = {row.name: row for row in rows}
    rows = list(unique_rows.values())
    tokens = [token for token in query.split() if len(token) >= 2]
    matches = []
    for row in rows:
        content = row.content or ""
        lowered = content.lower()
        if agent and f"agent={agent}" in content:
            pass
        elif company and f"company={company}" in content:
            pass
        elif row.scope != "User":
            continue
        score = sum(1 for token in tokens if token in lowered)
        if query in lowered:
            score += 5
        if score:
            matches.append((score, row))

    matches.sort(key=lambda item: (item[0], item[1].modified), reverse=True)
    result_rows = [
        {
            "scope": row.scope,
            "agent": row.get("agent"),
            "memory_type": row.memory_type,
            "content": _clean(row.content, 700),
            "modified": row.modified,
        }
        for _, row in matches[:limit]
    ]
    return {
        "rows": result_rows,
        "row_count": len(result_rows),
        "candidate_count": len(rows),
        "exhaustive": not rows,
        "message": _("No AI memory exists for the current user, agent, and company scope.") if not rows else None,
    }


def handle_store_context(arguments, permission_context):
    """Store a compact reusable context entry for future runs."""
    content = _clean(arguments.get("content"), 900)
    if not content:
        return {"error": "content is required"}

    service = MemoryService()
    if not service.is_enabled():
        return {"stored": False, "message": _("AI memory is disabled.")}

    scope = arguments.get("scope") or "Agent"
    if scope not in {"User", "Agent", "Company"}:
        return {"error": "scope must be User, Agent, or Company"}

    company = getattr(permission_context, "company", None) or arguments.get("company")
    agent = getattr(permission_context, "agent_name", None) or arguments.get("agent")
    if scope == "Agent" and not agent:
        return {"error": "agent is required when scope is Agent"}
    tagged_content = content
    if agent and "agent=" not in tagged_content:
        tagged_content = f"agent={agent}; {tagged_content}"
    if company and "company=" not in tagged_content:
        tagged_content = f"company={company}; {tagged_content}"

    memory = service.store(
        user=permission_context.user,
        scope=scope,
        memory_type=arguments.get("memory_type") or "Context",
        content=tagged_content,
        company=company,
        agent=agent,
        sensitivity=arguments.get("sensitivity") or "Medium",
    )
    return {
        "stored": bool(memory),
        "memory": memory.name if memory else None,
        "content_preview": _clean(tagged_content, 300),
    }


def _clean(value, limit):
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()
