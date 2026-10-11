"""Build a Provider from an AI Provider record (Frappe-aware)."""

from __future__ import annotations

import json

import frappe

from ai_agent.engine.providers.anthropic import AnthropicProvider
from ai_agent.engine.providers.openai_compat import OpenAICompatProvider
from ai_agent.runtime import policy


def anthropic_base_url(url):
    """The Anthropic SDK adds /v1/... itself, so a base URL ending in /v1 (as OpenAI-style
    URLs do) would call /v1/v1/models and get 404. Keep only the host part; empty stays empty
    (the SDK then uses https://api.anthropic.com)."""
    url = (url or "").strip().rstrip("/")
    for suffix in ("/v1/messages", "/v1"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def provider_config(provider_doc, model=None):
    settings = frappe.get_cached_doc("AI Settings")
    models = {}
    for row in provider_doc.get("models") or []:
        models[row.model_name] = row.as_dict() if hasattr(row, "as_dict") else dict(row)
    extra = {}
    raw = provider_doc.get("extra_request_json")
    if raw:
        try:
            extra = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except ValueError:
            frappe.log_error(f"AI Provider {provider_doc.name}: extra_request_json is not valid JSON", "AI Provider")
    from ai_agent.ai_agent.doctype.ai_provider.ai_provider import chat_capable

    # Only models that can run the chat (a chat model that takes tools) are picked
    # automatically; the rows are sorted by name, so "first row" alone could land on
    # e.g. an agent-only or speech model.
    usable = [r for r in provider_doc.get("models") or [] if chat_capable(r)]
    default_model = provider_doc.get("default_model") or next((r.model_name for r in usable if r.is_default), None) \
        or next((r.model_name for r in usable), None) or settings.default_model
    if model and model in models and not chat_capable(models[model]):
        model = None   # a session left on a model that cannot chat falls back to the default
    ptype = provider_doc.get("provider_type") or "OpenAI Compatible"
    base_url = (provider_doc.get("base_url") or "").strip()
    if ptype == "OpenAI" and not base_url:
        base_url = "https://api.openai.com/v1"
    elif ptype == "Ollama" and not base_url:
        base_url = "http://localhost:11434/v1"
    elif ptype == "Anthropic":
        base_url = anthropic_base_url(base_url)
    try:
        api_key = provider_doc.get_password("api_key") if provider_doc.get("api_key") else ""
    except Exception:
        api_key = ""
    return {
        "name": provider_doc.name,
        "type": ptype,
        "base_url": base_url,
        "api_key": api_key or "",
        "timeout": provider_doc.get("request_timeout") or settings.request_timeout or 120,
        "model": model or default_model or "",
        "models": models,
        "strict_tools": bool(provider_doc.get("strict_tool_calling")),
        "extra_request": extra,
        "default_effort": provider_doc.get("default_reasoning_effort") or settings.get("default_reasoning_effort") or "",
    }


def validate_provider_model(provider, model):
    """For agent / support settings: a picked model must belong to the picked provider."""
    if not provider:
        return
    if not frappe.db.exists("AI Provider", provider):
        frappe.throw(frappe._("AI Provider {0} not found.").format(provider))
    if model:
        known = set(frappe.get_all("AI Provider Model", filters={"parent": provider, "parenttype": "AI Provider"}, pluck="model_name"))
        if known and model not in known:
            frappe.throw(frappe._("Model {0} is not configured for provider {1}.").format(model, provider))


def resolve_provider_doc(provider_name=None):
    if provider_name:
        if not frappe.db.exists("AI Provider", {"name": provider_name, "enabled": 1}):
            frappe.throw(frappe._("AI Provider {0} is not available.").format(provider_name))
        return frappe.get_cached_doc("AI Provider", provider_name)
    default_name = frappe.db.get_value("AI Provider", {"is_default": 1, "enabled": 1}, "name") \
        or frappe.db.get_value("AI Provider", {"enabled": 1}, "name")
    if not default_name:
        frappe.throw(frappe._("No enabled AI Provider is configured."))
    return frappe.get_cached_doc("AI Provider", default_name)


def provider_for(provider_name=None, model=None):
    """Provider instance plus the resolved (provider name, model)."""
    doc = resolve_provider_doc(provider_name)
    policy.assert_provider_allowed(doc.provider_type or "OpenAI Compatible")
    config = provider_config(doc, model)
    if (doc.provider_type or "") == "Anthropic":
        provider = AnthropicProvider(config)
    else:
        provider = OpenAICompatProvider(config)
    return provider, doc.name, config["model"]
