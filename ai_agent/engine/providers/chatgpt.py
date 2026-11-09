"""ChatGPT Subscription: the person's own ChatGPT plan through OpenAI's Responses API
(developers.openai.com/siwc/token-sharing-open-source/models-and-inference and .../preview-limitations).

- every request carries the person's own OAuth access token (chatgpt/account.py refreshes it); the run is
  executed as that person, so `frappe.session.user` is whose plan answers;
- `store: false`, `stream: true`, the whole history in `input`, the system prompt in `instructions`; the fields the
  route rejects (temperature, max_output_tokens, user, …) are never sent;
- the tools go in one namespace ("Group function/custom tools in namespaces"); a server that refuses that gets
  them as plain function tools, once per process;
- usage-limit and eligibility errors keep their code, so the chat can say what happened (engine/errors.py).
"""

from __future__ import annotations

import json

import frappe

from ai_agent.engine.events import MessageStop, ProviderError, UsageEvent
from ai_agent.engine.providers.base import BlockAssembler, CancelToken, Provider, classify_error
from ai_agent.engine.types import (
    CANCELLED,
    DOCUMENT,
    END_TURN,
    IMAGE,
    MAX_TOKENS,
    SKILL,
    TEXT,
    THINKING,
    TOOL_RESULT,
    TOOL_USE,
    TOOL_USE_STOP,
    ModelRequest,
    Usage,
)

NAMESPACE = "erp"
_plain_tools = set()   # provider names whose server refused the namespace: plain function tools there


class ChatGPTProvider(Provider):
    name = "chatgpt"

    def client(self, token):
        from openai import OpenAI

        from ai_agent.chatgpt.account import RESOURCE

        return OpenAI(api_key=token, base_url=RESOURCE, timeout=self.timeout, max_retries=0)

    # -- request --------------------------------------------------------------

    def build_request(self, request: ModelRequest, plain_tools=False):
        payload = {
            "model": request.model or self.model,
            "instructions": "\n\n".join(p for p in (request.system, request.system_dynamic) if p),
            "input": convert_input(request.messages, namespaced=not plain_tools),
            "store": False,
            "stream": True,
        }
        if request.tools:
            functions = [{"type": "function", "name": t.name, "description": t.description or "",
                          "parameters": t.parameters or {"type": "object", "properties": {}}} for t in request.tools]
            payload["tools"] = functions if plain_tools else [
                {"type": "namespace", "name": NAMESPACE, "description": "This ERP site's tools: data, reports, documents and approvals.", "tools": functions}]
        effort = (request.effort or self.config.get("default_effort") or "").lower()
        if effort in ("minimal", "low", "medium", "high"):
            payload["reasoning"] = {"effort": effort}
        return payload

    # -- stream ---------------------------------------------------------------

    def stream(self, request: ModelRequest, cancel: CancelToken):
        from ai_agent.chatgpt import account

        try:
            token = account.access_token(frappe.session.user)
        except account.SignInRequired as exc:
            yield ProviderError(message=str(exc), kind="error", retryable=False)
            return
        except Exception as exc:
            yield _error(exc)
            return
        plain = self.config.get("name") in _plain_tools
        try:
            payload = self.build_request(request, plain_tools=plain)
            raw = self.client(token).responses.create(**payload)
        except Exception as exc:
            if not plain and request.tools and _refuses_namespace(exc):
                _plain_tools.add(self.config.get("name"))
                try:
                    payload = self.build_request(request, plain_tools=True)
                    raw = self.client(token).responses.create(**payload)
                except Exception as again:
                    yield _error(again, token)
                    return
            else:
                yield _error(exc, token)
                return

        asm = BlockAssembler()
        calls = {}        # output_index -> block seq
        usage = Usage()
        stop = END_TURN
        try:
            for event in raw:
                if cancel.cancelled:
                    try:
                        raw.close()
                    except Exception:
                        pass   # closing a cancelled stream is best-effort
                    for ev in asm.close_all():
                        yield ev
                    yield MessageStop(stop_reason=CANCELLED)
                    return
                kind = getattr(event, "type", "")
                if kind == "response.output_text.delta":
                    for ev in asm.close_kind_except(TEXT):
                        yield ev
                    seq, started = asm.ensure(TEXT)
                    if started:
                        yield started
                    yield asm.delta(seq, event.delta)
                elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                    for ev in asm.close_kind_except(THINKING):
                        yield ev
                    seq, started = asm.ensure(THINKING)
                    if started:
                        yield started
                    yield asm.delta(seq, event.delta)
                elif kind == "response.output_item.added" and getattr(event.item, "type", "") == "function_call":
                    for ev in asm.close_all():
                        yield ev
                    item = event.item
                    started = asm.start(TOOL_USE, id=item.call_id, name=_tool_name(item))
                    if getattr(item, "namespace", None):
                        asm.signature(started.block_seq, f"ns:{item.namespace}")
                    calls[event.output_index] = started.block_seq
                    yield started
                    if getattr(item, "arguments", ""):
                        yield asm.delta(started.block_seq, item.arguments)
                elif kind == "response.function_call_arguments.delta":
                    seq = calls.get(event.output_index)
                    if seq is not None:
                        yield asm.delta(seq, event.delta)
                elif kind == "response.output_item.done" and getattr(event.item, "type", "") == "function_call":
                    seq = calls.get(event.output_index)
                    if seq is not None and not asm._open.get(seq, {}).get("json") and getattr(event.item, "arguments", ""):
                        yield asm.delta(seq, event.item.arguments)
                elif kind == "response.completed":
                    usage = _usage(event.response)
                elif kind == "response.incomplete":
                    usage = _usage(event.response)
                    stop = MAX_TOKENS
                elif kind in ("response.failed", "error"):
                    err = getattr(getattr(event, "response", None), "error", None) or event
                    code, message = getattr(err, "code", "") or "", getattr(err, "message", "") or ""
                    for ev in asm.close_all():
                        yield ev
                    yield ProviderError(message=f"{code}: {message}".strip(": ")[:600], kind=_kind(code), retryable=_retryable(code))
                    return
        except Exception as exc:
            for ev in asm.close_all():
                yield ev
            yield _error(exc, token)
            return

        for ev in asm.close_all():
            yield ev
        yield UsageEvent(usage=usage.to_dict(), context_tokens=usage.context_size(), model=payload["model"])
        if stop == END_TURN and any(b.kind == TOOL_USE for b in asm.blocks):
            stop = TOOL_USE_STOP
        yield MessageStop(stop_reason=stop)


# -- helpers ------------------------------------------------------------------

def _tool_name(item):
    name = item.name or ""
    namespace = getattr(item, "namespace", None) or NAMESPACE
    return name[len(namespace) + 1:] if name.startswith(namespace + ".") else name


def _usage(response):
    u = getattr(response, "usage", None)
    if not u:
        return Usage()
    cached = int(getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0)
    return Usage(input_tokens=max(0, int(u.input_tokens or 0) - cached), output_tokens=int(u.output_tokens or 0),
                 cache_read_input_tokens=cached, cache_creation_input_tokens=0)


LIMIT_CODES = ("subscription_sharing_usage_limit_exceeded",)
RETRY_CODES = ("subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable")


def _kind(code):
    return "rate_limit" if code in LIMIT_CODES else ("server" if code in RETRY_CODES else "error")


def _retryable(code):
    return code in RETRY_CODES


def _body_code(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        return error.get("code") or "", error.get("param") or "", error.get("message") or body.get("detail") or ""
    return "", "", ""


def _refuses_namespace(exc):
    code, param, message = _body_code(exc)
    text = f"{code} {param} {message} {exc}".lower()
    return "namespace" in text or (code == "subscription_sharing_unsupported_capability" and "tool" in param.lower())


def _error(exc, token=None):
    code, _param, message = _body_code(exc)
    kind, retryable = classify_error(exc)
    if code:
        kind, retryable = (_kind(code) if code in LIMIT_CODES + RETRY_CODES else kind), (_retryable(code) or retryable)
    text = (f"{code}: {message}" if code else str(exc) or exc.__class__.__name__)
    if token:
        text = text.replace(token, "***")
    return ProviderError(message=text[:600], kind=kind, retryable=retryable)


def convert_input(messages, namespaced=True):
    """Native blocks -> Responses input items: messages, function calls and their outputs. Thinking is not replayed."""
    out = []
    for m in messages:
        if m.role == "user":
            for b in m.blocks:
                if b.kind == TOOL_RESULT:
                    out.append({"type": "function_call_output", "call_id": b.id, "output": _result_text(b)})
            parts = []
            for b in m.blocks:
                if b.kind in (TEXT, SKILL) and b.text:
                    parts.append({"type": "input_text", "text": b.text})
                elif b.kind == DOCUMENT:
                    parts.append({"type": "input_text", "text": b.text or f"[attached document: {b.name}]"})
                elif b.kind == IMAGE and b.data.get("data"):
                    parts.append({"type": "input_image", "image_url": f"data:{b.data.get('media_type', 'image/png')};base64,{b.data['data']}"})
            if parts:
                out.append({"role": "user", "content": parts})
        elif m.role == "assistant":
            text = "".join(b.text for b in m.blocks if b.kind == TEXT)
            if text:
                out.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for b in m.blocks:
                if b.kind != TOOL_USE:
                    continue
                call = {"type": "function_call", "call_id": b.id or f"call_{b.seq}", "name": b.name,
                        "arguments": json.dumps(b.data, ensure_ascii=False)}
                if namespaced and (b.signature or "").startswith("ns:"):
                    call["namespace"] = b.signature[3:]
                out.append(call)
    return out


def _result_text(block):
    if block.text:
        return block.text
    if block.data:
        return json.dumps(block.data, ensure_ascii=False, default=str)
    return "(empty)"
