from frappe import _

from ai_agent.runtime.memory_service import MemoryService, normalize_entity_key


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
        rows.extend(service.retrieve(
            user=permission_context.user,
            scope=scope,
            company=company,
            agent=agent,
        ))

    # retrieve() already scopes by the structured user/agent/company fields.
    unique_rows = {row.name: row for row in rows}
    rows = list(unique_rows.values())
    normalized_query = normalize_entity_key(query)
    tokens = [token for token in normalized_query.split() if len(token) >= 2]
    matches = []
    for row in rows:
        normalized_content = normalize_entity_key(row.content or "")
        entity_key = row.get("entity_key") or ""
        score = sum(1 for token in tokens if token in normalized_content)
        if normalized_query in normalized_content:
            score += 5
        if entity_key and entity_key in normalized_query:
            score += 6
        if score:
            matches.append((score, row))

    matches.sort(key=lambda item: (item[0], str(item[1].modified or "")), reverse=True)
    result_rows = [
        {
            "scope": row.scope,
            "agent": row.get("agent"),
            "memory_type": row.memory_type,
            "entity_type": row.get("entity_type"),
            "entity_key": row.get("entity_key"),
            "mapping": row.get("mapping_json"),
            "content": _clean(row.content, 700),
            "modified": row.modified,
        }
        for _, row in matches[:limit]
    ]
    return {
        "rows": result_rows,
        "row_count": len(result_rows),
        "candidate_count": len(rows),
        # Never flag "exhaustive" here: the orchestrator treats it as a
        # finalize-now signal, and an empty memory must not end the task.
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

    mapping = arguments.get("mapping")
    memory = service.store(
        user=permission_context.user,
        scope=scope,
        memory_type=arguments.get("memory_type") or "Context",
        content=tagged_content,
        company=company,
        agent=agent,
        sensitivity=arguments.get("sensitivity") or "Medium",
        entity_type=arguments.get("entity_type"),
        entity_key=arguments.get("entity_key"),
        mapping=mapping if isinstance(mapping, dict) else None,
    )
    return {
        "stored": bool(memory),
        "memory": memory.name if memory else None,
        "content_preview": _clean(tagged_content, 300),
    }


def _clean(value, limit):
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()
