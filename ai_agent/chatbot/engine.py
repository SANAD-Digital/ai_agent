from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import now_datetime

from ai_agent.chatbot import settings
from ai_agent.chatbot.setup import ensure_chatbot_agent, ensure_chatbot_skill_grants


CONVERSATION_DOCTYPE = "AI Chatbot Conversation"
MESSAGE_DOCTYPE = "AI Chatbot Message"
TERMINAL_RUN_STATUSES = {"Completed", "Failed", "Cancelled", "Timed Out"}

# Streaming state is kept in the Redis-backed cache so it is visible
# from background worker processes that generate token-by-token output.
STREAM_KEY_PREFIX = "ai_chatbot_stream"
STREAM_LIST_KEY = STREAM_KEY_PREFIX + "_list"
STREAM_TTL_SECONDS = 3600
STREAM_STATUS_KEY = STREAM_KEY_PREFIX + "_status_{stream_id}"
STREAM_CONTENT_KEY = STREAM_KEY_PREFIX + "_content_{stream_id}"

STREAM_STATUS_STARTED = "started"
STREAM_STATUS_STREAMING = "streaming"
STREAM_STATUS_COMPLETED = "completed"
STREAM_STATUS_FAILED = "failed"


def get_config(user=None):
    """Configuration payload for the floating widget."""
    user = user or frappe.session.user
    if not settings.is_enabled():
        return {"enabled": False}
    allowed = can_access(user)
    return {
        "enabled": True,
        "can_access": allowed,
        "title": settings.widget_title(),
        "welcome_message": settings.welcome_message(),
        "conversation": _find_open_conversation(user) if allowed else None,
        "problem_solving": settings.enable_problem_solving(),
    }


def _find_open_conversation(user):
    if not frappe.db.exists(CONVERSATION_DOCTYPE):
        return None
    return frappe.db.get_value(
        CONVERSATION_DOCTYPE,
        {"user": user, "status": "Open"},
        "name",
        order_by="modified desc",
    )


def can_access(user=None) -> bool:
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    allowed_roles = settings.allowed_roles()
    if not allowed_roles:
        return True
    return bool(set(frappe.get_roles(user)) & set(allowed_roles))


def get_or_create_conversation(user=None) -> str:
    user = user or frappe.session.user
    if frappe.db.exists(CONVERSATION_DOCTYPE):
        existing = frappe.db.get_value(
            CONVERSATION_DOCTYPE,
            {"user": user, "status": "Open"},
            "name",
            order_by="modified desc",
        )
        if existing:
            return existing
    conversation = frappe.get_doc(
        {
            "doctype": CONVERSATION_DOCTYPE,
            "user": user,
            "status": "Open",
            "company": settings.company(),
            "message_count": 0,
        }
    )
    conversation.insert(ignore_permissions=True)
    frappe.db.commit()
    return conversation.name


def get_messages(conversation: str) -> list[dict]:
    if not frappe.db.exists(CONVERSATION_DOCTYPE, conversation):
        return []
    doc = frappe.get_doc(CONVERSATION_DOCTYPE, conversation)
    _assert_conversation_owner(doc)
    return [
        {
            "role": row.role,
            "content": row.content or "",
            "ai_run": row.ai_run,
            "timestamp": str(row.timestamp or row.creation or ""),
        }
        for row in doc.messages
    ]


def send_message(message: str, conversation: str | None = None):
    message = str(message or "").strip()
    if not message:
        frappe.throw(_("الرجاء كتابة رسالة قبل الإرسال."))
    if len(message) > 20000:
        message = message[:20000]
    if not settings.is_enabled():
        frappe.throw(_("الدردشة معطلة حاليًا."))
    if not can_access():
        frappe.throw(_("ليس لديك صلاحية استخدام هذه الدردشة."))

    conversation_name = conversation or get_or_create_conversation()
    doc = frappe.get_doc(CONVERSATION_DOCTYPE, conversation_name)
    _assert_conversation_owner(doc)

    if not doc.subject:
        doc.subject = message[:100]

    _append_message(doc, "User", message, None)

    try:
        if settings.enable_problem_solving():
            answer, run_name = _agent_answer(message, doc)
        else:
            answer, run_name = _chat_answer(message, doc)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Chatbot Engine Failed")
        answer = settings.error_reply()
        run_name = None

    answer = _clean_answer(answer)
    if not answer:
        answer = settings.error_reply()

    _append_message(doc, "Assistant", answer, run_name)
    return {
        "conversation": conversation_name,
        "role": "Assistant",
        "content": answer,
        "ai_run": run_name,
    }


def _agent_answer(message: str, conversation_doc) -> tuple[str, str | None]:
    agent = _resolve_agent()
    company = (
        settings.company()
        or frappe.defaults.get_user_default("Company")
        or frappe.db.get_default("Company")
    )

    run = frappe.get_doc(
        {
            "doctype": "AI Run",
            "agent": agent,
            "user": conversation_doc.user,
            "company": company,
            "prompt": message,
            "status": "Queued",
            "parent_run": conversation_doc.last_run or None,
            "started_at": now_datetime(),
        }
    )
    run.insert(ignore_permissions=True)
    frappe.db.commit()

    from ai_agent.runtime.orchestrator import execute_run

    execute_run(run.name)
    run.reload()

    if run.status == "Completed" and run.final_answer:
        return run.final_answer, run.name

    frappe.log_error(
        f"Chatbot AI Run {run.name} ended with status {run.status}: {run.error_summary or ''}",
        "Chatbot Agent Run",
    )
    return "", run.name


def _chat_answer(message: str, conversation_doc) -> tuple[str, None]:
    from ai_agent.runtime.model_provider import get_provider

    provider = get_provider()
    system_prompt = settings.system_prompt()

    history = _history_for_chat(conversation_doc, settings.max_history_messages())
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    instructions = {
        "messages": messages,
        "model": settings.model() or _agent_model(),
        "reasoning_effort": settings.reasoning_effort(),
        "max_tokens": 4096,
    }
    temperature = settings.temperature()
    if temperature is not None:
        instructions["temperature"] = temperature

    response = provider.create_step(
        instructions=instructions,
        input_items=[],
        tools=[],
        state={"chatbot_direct": True},
    )
    choice = (response or {}).get("choices", [{}])[0] if isinstance(response, dict) else {}
    content = (choice.get("message", {}) or {}).get("content") or ""
    return content, None


def _history_for_chat(conversation_doc, limit: int):
    rows = list(conversation_doc.messages)[-max(limit, 1):]
    history = []
    for row in rows:
        role = str(row.role or "").lower()
        if role not in {"user", "assistant"}:
            continue
        if not (row.content or "").strip():
            continue
        history.append({"role": "user" if role == "user" else "assistant", "content": row.content})
    return history[-max(limit, 1):]


def _resolve_agent() -> str:
    name = settings.agent_name()
    if frappe.db.exists("AI Agent", name) or frappe.db.exists("AI Agent", {"agent_name": name}):
        agent = frappe.get_doc("AI Agent", name)
        changed = False
        model = settings.model()
        if model and agent.model != model:
            agent.model = model
            changed = True
        reasoning_effort = settings.reasoning_effort()
        if reasoning_effort and agent.reasoning_effort != reasoning_effort:
            agent.reasoning_effort = reasoning_effort
            changed = True
        if changed:
            agent.save(ignore_permissions=True)
        ensure_chatbot_skill_grants(agent.name)
        return agent.name

    return ensure_chatbot_agent()


def _agent_model() -> str:
    if frappe.db.exists("DocType", "AI Settings"):
        value = frappe.db.get_single_value("AI Settings", "default_model")
        if value:
            return value
    return "gpt-4o"


def _append_message(conversation_doc, role: str, content: str, run_name: str | None):
    conversation_doc.append(
        "messages",
        {
            "role": role,
            "content": content,
            "ai_run": run_name,
            "timestamp": now_datetime(),
        },
    )
    conversation_doc.message_count = len(conversation_doc.messages)
    conversation_doc.last_message = content[:500]
    if run_name:
        conversation_doc.last_run = run_name
    conversation_doc.save(ignore_permissions=True)
    frappe.db.commit()
    conversation_doc.reload()


def _assert_conversation_owner(conversation_doc):
    if frappe.session.user == "Administrator":
        return
    if conversation_doc.user != frappe.session.user:
        frappe.throw(_("ليس لديك صلاحية الوصول لهذه المحادثة."), frappe.PermissionError)


def _clean_answer(answer: str) -> str:
    # Preserve the assistant's Markdown as-is (do not strip HTML/tables).
    text = str(answer or "").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Keep large answers intact; only guard against absurdly large payloads.
    if len(text) > 80000:
        text = text[:80000]
    return text


def begin_stream_message(message: str, conversation: str | None = None, user: str | None = None):
    """Start generating an assistant reply in the background.

    Appends the user message, kicks off a background job that streams the
    reply into the cache, and returns a stream_id that the widget polls for
    the progressively growing answer.
    """
    message = str(message or "").strip()
    if not message:
        frappe.throw(_("الرجاء كتابة رسالة قبل الإرسال."))
    if len(message) > 20000:
        message = message[:20000]
    if not settings.is_enabled():
        frappe.throw(_("الدردشة معطلة حاليًا."))
    user = user or frappe.session.user
    if not can_access(user):
        frappe.throw(_("ليس لديك صلاحية استخدام هذه الدردشة."))

    conversation_name = conversation or get_or_create_conversation(user)
    doc = frappe.get_doc(CONVERSATION_DOCTYPE, conversation_name)
    _assert_conversation_owner(doc)

    if not doc.subject:
        doc.subject = message[:100]

    _append_message(doc, "User", message, None)

    stream_id = _new_stream_id()
    _set_stream_status(stream_id, STREAM_STATUS_STARTED)
    _set_stream_content(stream_id, "")
    _register_stream(stream_id)

    frappe.enqueue(
        "ai_agent.chatbot.engine._stream_generate_job",
        queue="default",
        timeout=1800,
        stream_id=stream_id,
        message=message,
        conversation=conversation_name,
        user=user,
    )

    return {
        "stream_id": stream_id,
        "conversation": conversation_name,
    }


def get_stream_content(stream_id: str):
    """Return the current accumulated answer for a stream plus its status."""
    status = _get_stream_status(stream_id) or STREAM_STATUS_FAILED
    content = _get_stream_content(stream_id) or ""
    return {
        "stream_id": stream_id,
        "status": status,
        "content": content,
    }


def _stream_generate_job(stream_id: str, message: str, conversation: str, user: str):
    """Background job: stream a chat reply token-by-token into the cache."""
    try:
        doc = frappe.get_doc(CONVERSATION_DOCTYPE, conversation)
        _assert_conversation_owner(doc)
        _set_stream_status(stream_id, STREAM_STATUS_STREAMING)

        if settings.enable_problem_solving():
            answer, run_name = _agent_answer(message, doc)
            _set_stream_content(stream_id, answer)
        else:
            answer = _stream_chat_answer(stream_id, message, doc)
            run_name = None

        answer = _clean_answer(answer)
        if not answer:
            answer = settings.error_reply()

        _set_stream_content(stream_id, answer)
        _set_stream_status(stream_id, STREAM_STATUS_COMPLETED)
        _append_message(doc, "Assistant", answer, run_name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Chatbot Streaming Failed")
        _set_stream_status(stream_id, STREAM_STATUS_FAILED)
        _set_stream_content(stream_id, settings.error_reply())


def _stream_chat_answer(stream_id: str, message: str, conversation_doc) -> str:
    """Stream a direct chat reply, writing each accumulated chunk to the cache."""
    from ai_agent.runtime.model_provider import get_provider

    provider = get_provider()
    system_prompt = settings.system_prompt()

    history = _history_for_chat(conversation_doc, settings.max_history_messages())
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    instructions = {
        "messages": messages,
        "model": settings.model() or _agent_model(),
        "reasoning_effort": settings.reasoning_effort(),
        "max_tokens": 4096,
    }
    temperature = settings.temperature()
    if temperature is not None:
        instructions["temperature"] = temperature

    if not hasattr(provider, "create_step_stream"):
        # Provider does not support streaming; fall back to a regular call.
        response = provider.create_step(
            instructions=instructions,
            input_items=[],
            tools=[],
            state={"chatbot_direct": True},
        )
        choice = (response or {}).get("choices", [{}])[0] if isinstance(response, dict) else {}
        content = (choice.get("message", {}) or {}).get("content") or ""
        _set_stream_content(stream_id, content)
        return content

    full = ""
    for chunk in provider.create_step_stream(
        instructions=instructions,
        input_items=[],
        tools=[],
        state={"chatbot_direct": True},
    ):
        full = chunk
        _set_stream_content(stream_id, full)
    return full


def _new_stream_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _stream_cache_key(template: str, stream_id: str) -> str:
    return template.format(stream_id=stream_id)


def _set_stream_status(stream_id: str, status: str):
    key = _stream_cache_key(STREAM_STATUS_KEY, stream_id)
    frappe.cache().set_value(key, status, expires_in_sec=STREAM_TTL_SECONDS)


def _get_stream_status(stream_id: str) -> str:
    key = _stream_cache_key(STREAM_STATUS_KEY, stream_id)
    try:
        return frappe.cache().get_value(key) or STREAM_STATUS_STARTED
    except Exception:
        return STREAM_STATUS_STARTED


def _set_stream_content(stream_id: str, content: str):
    key = _stream_cache_key(STREAM_CONTENT_KEY, stream_id)
    frappe.cache().set_value(key, content, expires_in_sec=STREAM_TTL_SECONDS)


def _get_stream_content(stream_id: str) -> str:
    key = _stream_cache_key(STREAM_CONTENT_KEY, stream_id)
    try:
        return frappe.cache().get_value(key) or ""
    except Exception:
        return ""


def _register_stream(stream_id: str):
    try:
        current = frappe.cache().get_value(STREAM_LIST_KEY) or []
        if stream_id not in current:
            current.append(stream_id)
            frappe.cache().set_value(STREAM_LIST_KEY, current, expires_in_sec=STREAM_TTL_SECONDS)
    except Exception:
        pass
