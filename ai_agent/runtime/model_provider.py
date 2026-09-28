import json
import frappe
from ai_agent.runtime.exceptions import ModelProviderError


DEFAULT_CONTEXT_WINDOW_TOKENS = 128000


class AgentModelProvider:
    """Interface for AI model providers."""

    def create_step(self, *, instructions, input_items, tools, state):
        raise NotImplementedError

    def continue_step(self, *, response_id, tool_outputs, tools, state):
        raise NotImplementedError

    def get_usage(self, response):
        raise NotImplementedError

    def get_context_window(self, model=None):
        return estimate_context_window_tokens(model)


class OpenAICompatibleProvider(AgentModelProvider):
    """Provider for OpenAI and OpenAI-compatible APIs."""

    def __init__(self, settings=None):
        if settings is None:
            settings = frappe.get_cached_doc("AI Settings")
        self.settings = settings
        self.base_url = self._get_base_url(settings)
        self.api_key = self._get_api_key(settings)
        self.model = settings.default_model or "gpt-4o"

    def _get_base_url(self, settings):
        provider_type = settings.provider_type or "OpenAI"
        if provider_type == "OpenAI":
            return "https://api.openai.com/v1"
        elif provider_type == "Ollama":
            return settings.base_url or "http://localhost:11434/v1"
        else:
            return settings.base_url or "https://api.openai.com/v1"

    def _get_api_key(self, settings):
        try:
            return settings.get_password("api_key")
        except Exception:
            return ""

    def create_step(self, *, instructions, input_items, tools, state):
        """Create a new model step."""
        import requests

        messages = _openai_messages(instructions.get("messages", []))
        model = instructions.get("model", self.model)
        is_deepseek = self._is_deepseek_model(model)

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": instructions.get("max_tokens") or 4096,
        }
        if instructions.get("response_format"):
            payload["response_format"] = instructions.get("response_format")

        if instructions.get("temperature") is not None:
            payload["temperature"] = instructions["temperature"]

        if tools:
            payload["tools"] = self._format_tools(
                tools,
                strict=bool(self.settings.get("strict_tool_calling")) and not is_deepseek,
            )
            payload["tool_choice"] = "auto"

        reasoning_effort = self._normalize_reasoning_effort(
            instructions.get("reasoning_effort") or self.settings.get("default_reasoning_effort"),
            is_deepseek=is_deepseek,
        )
        if reasoning_effort and not is_deepseek:
            payload["reasoning_effort"] = reasoning_effort

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = instructions.get("timeout") or self.settings.request_timeout or 30

        import time
        try:
            max_retries = int(instructions.get("max_retries") or 2)
        except Exception:
            max_retries = 2
        max_retries = max(1, max_retries)
        last_exception = None

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )
                if resp.status_code == 429:
                    if attempt < max_retries - 1:
                        retry_header = resp.headers.get("Retry-After")
                        try:
                            retry_after = int(retry_header) if retry_header and retry_header.isdigit() else 5
                        except Exception:
                            retry_after = 5
                        time.sleep(min(retry_after, 10))
                        continue
                if resp.status_code >= 400:
                    if attempt < max_retries - 1 and resp.status_code in (429, 500, 502, 503, 504):
                        retry_header = resp.headers.get("Retry-After")
                        try:
                            retry_after = int(retry_header) if retry_header and retry_header.isdigit() else 5
                        except Exception:
                            retry_after = 5
                        time.sleep(min(retry_after, 10))
                        continue
                    raise ModelProviderError(self._format_provider_error(resp))
                return resp.json()
            except ModelProviderError as e:
                last_exception = e
                break
            except requests.exceptions.Timeout:
                last_exception = ModelProviderError("Model provider request timed out.")
                if attempt < max_retries - 1:
                    time.sleep(min(2 ** (attempt + 1), 10))
                    continue
            except requests.exceptions.RequestException as e:
                last_exception = ModelProviderError(f"Model provider error: {str(e)}")
                # Retry on 429 or 5xx server errors
                status_code = getattr(e.response, 'status_code', None) if e.response is not None else None
                if attempt < max_retries - 1 and (status_code is None or status_code in (429, 500, 502, 503, 504)):
                    time.sleep(min(5 * (2 ** attempt), 10))
                    continue
            except Exception as e:
                last_exception = ModelProviderError(f"Unexpected error: {str(e)}")
                break

        raise last_exception

    def create_step_stream(self, *, instructions, input_items, tools, state):
        """Stream a model step token by token.

        Yields the accumulated assistant content string as it grows at each
        non-empty delta, and returns the final full content string on completion.
        """
        import requests

        messages = _openai_messages(instructions.get("messages", []))
        model = instructions.get("model", self.model)
        is_deepseek = self._is_deepseek_model(model)

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": instructions.get("max_tokens") or 4096,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if instructions.get("temperature") is not None:
            payload["temperature"] = instructions["temperature"]

        if tools:
            payload["tools"] = self._format_tools(
                tools,
                strict=bool(self.settings.get("strict_tool_calling")) and not is_deepseek,
            )
            payload["tool_choice"] = "auto"

        reasoning_effort = self._normalize_reasoning_effort(
            instructions.get("reasoning_effort") or self.settings.get("default_reasoning_effort"),
            is_deepseek=is_deepseek,
        )
        if reasoning_effort and not is_deepseek:
            payload["reasoning_effort"] = reasoning_effort

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = self.settings.request_timeout or 30

        try:
            with requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                stream=True,
                timeout=timeout,
            ) as resp:
                if resp.status_code >= 400:
                    raise ModelProviderError(self._format_provider_error(resp))
                content_parts = []
                for line in resp.iter_lines(decode_unicode=False):
                    if not line:
                        continue
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        content_parts.append(delta)
                        yield "".join(content_parts)
                return "".join(content_parts)
        except ModelProviderError:
            raise
        except requests.exceptions.RequestException as e:
            raise ModelProviderError(f"Model provider error during streaming: {str(e)}")
        except Exception as e:
            raise ModelProviderError(f"Unexpected error during streaming: {str(e)}")

    def _format_provider_error(self, response):
        detail = ""
        try:
            data = response.json()
            if isinstance(data, dict):
                error = data.get("error") or data
                if isinstance(error, dict):
                    detail = error.get("message") or error.get("code") or json.dumps(error, ensure_ascii=False)
                else:
                    detail = str(error)
        except Exception:
            detail = response.text or ""
        detail = str(detail or "").strip()
        if len(detail) > 800:
            detail = f"{detail[:800]}..."
        message = f"Model provider error: {response.status_code} {response.reason}"
        if detail:
            message = f"{message} - {detail}"
        return message

    def continue_step(self, *, response_id, tool_outputs, tools, state):
        """Continue a model step with tool outputs."""
        messages = state.get("messages", [])
        for output in tool_outputs:
            messages.append({
                "role": "tool",
                "tool_call_id": output["call_id"],
                "content": json.dumps(output["result"], default=str)[:8000],
            })

        return self.create_step(
            instructions={
                "messages": messages,
                "model": state.get("model", self.model),
                "reasoning_effort": state.get("reasoning_effort"),
            },
            input_items=[],
            tools=tools,
            state=state,
        )

    def get_usage(self, response):
        """Extract token usage from response."""
        usage = response.get("usage", {})
        input_tokens = (
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage.get("prompt_token_count")
            or 0
        )
        output_tokens = (
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage.get("candidates_token_count")
            or 0
        )
        total_tokens = usage.get("total_tokens") or usage.get("total_token_count")
        if total_tokens and not (input_tokens or output_tokens):
            input_tokens = total_tokens
        cached = (
            usage.get("prompt_cache_hit_tokens")  # DeepSeek
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")  # OpenAI
            or 0
        )
        return {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read_input_tokens": int(cached or 0),
            "cache_creation_input_tokens": 0,
        }

    def get_context_window(self, model=None):
        return estimate_context_window_tokens(model or self.model, self.base_url)

    def _is_deepseek_model(self, model):
        return "api.deepseek.com" in self.base_url or str(model).startswith("deepseek-")

    def _normalize_reasoning_effort(self, reasoning_effort, is_deepseek=False):
        value = str(reasoning_effort or "").strip().lower()
        if not value:
            return None
        allowed = {"low", "medium", "high"}
        if value not in allowed:
            return "medium" if is_deepseek else None
        if is_deepseek and value == "high":
            return "medium"
        return value

    def _format_tools(self, tool_schemas, strict=False):
        """Format tool schemas for OpenAI API."""
        formatted = []
        for tool in tool_schemas:
            function = {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
            }
            if strict:
                function["strict"] = True
            formatted.append({
                "type": "function",
                "function": function,
            })
        return formatted


def _openai_messages(messages):
    """Drop provider-private keys: Anthropic content blocks kept for replay and
    runtime bookkeeping keys starting with "_"."""
    return [
        {k: v for k, v in message.items() if k != "provider_blocks" and not k.startswith("_")}
        if isinstance(message, dict) else message
        for message in messages or []
    ]


class ProviderConfig:
    """Settings-shaped view over an AI Provider record, falling back to the
    global AI Settings for anything the provider leaves blank."""

    def __init__(self, provider_doc, settings):
        self._doc = provider_doc
        self.name = provider_doc.name
        self.provider_type = provider_doc.provider_type or "OpenAI Compatible"
        self.base_url = provider_doc.base_url or ""
        self.default_model = (
            provider_doc.default_model
            or provider_default_model(provider_doc)
            or settings.default_model
        )
        self.request_timeout = provider_doc.request_timeout or settings.request_timeout
        self.strict_tool_calling = (
            provider_doc.strict_tool_calling
            if provider_doc.strict_tool_calling is not None
            else settings.get("strict_tool_calling")
        )
        self.default_reasoning_effort = (
            provider_doc.default_reasoning_effort or settings.get("default_reasoning_effort")
        )

    def get(self, key, default=None):
        return getattr(self, key, default)

    def get_password(self, fieldname):
        return self._doc.get_password(fieldname)


def provider_default_model(provider_doc):
    if not provider_doc:
        return ""
    if provider_doc.default_model:
        return provider_doc.default_model
    rows = provider_doc.get("models") or []
    for row in rows:
        if row.is_default and row.model_name:
            return row.model_name
    return rows[0].model_name if rows and rows[0].model_name else ""


def _providers_available():
    return bool(frappe.db.exists("DocType", "AI Provider"))


def resolve_provider_doc(provider_name=None):
    """Explicit provider (must be enabled) or the default one; None means
    'use the legacy AI Settings connection'."""
    if not _providers_available():
        return None
    if provider_name:
        if not frappe.db.exists("AI Provider", {"name": provider_name, "enabled": 1}):
            raise ModelProviderError(f"AI Provider '{provider_name}' is not available.")
        return frappe.get_cached_doc("AI Provider", provider_name)
    default_name = frappe.db.get_value("AI Provider", {"is_default": 1, "enabled": 1}, "name")
    return frappe.get_cached_doc("AI Provider", default_name) if default_name else None


def resolve_run_model(run_doc=None, agent_doc=None):
    """Model precedence: run.model > (explicitly chosen provider's default) >
    agent.model > default provider's default > AI Settings."""
    settings = frappe.get_cached_doc("AI Settings")
    run_model = (run_doc.get("model") if run_doc else None) or ""
    if run_model.strip():
        return run_model.strip()
    explicit_provider = (run_doc.get("provider") if run_doc else None) or ""
    provider_doc = resolve_provider_doc(explicit_provider or None)
    if explicit_provider and provider_default_model(provider_doc):
        return provider_default_model(provider_doc)
    agent_model = (agent_doc.get("model") if agent_doc else None) or ""
    if agent_model.strip():
        return agent_model.strip()
    if provider_default_model(provider_doc):
        return provider_default_model(provider_doc)
    return settings.default_model


def resolve_run_base_url(run_doc=None):
    provider_doc = resolve_provider_doc((run_doc.get("provider") if run_doc else None) or None)
    if provider_doc and provider_doc.base_url:
        return provider_doc.base_url
    return frappe.get_cached_doc("AI Settings").base_url or ""


def get_provider(provider_name=None):
    """Build the model client for one AI Provider record (explicit or the
    default one), or for the legacy AI Settings connection when no provider
    records exist. Every supported type speaks the OpenAI chat protocol."""
    settings = frappe.get_cached_doc("AI Settings")
    provider_doc = resolve_provider_doc(provider_name)
    config = ProviderConfig(provider_doc, settings) if provider_doc else settings
    if (config.get("provider_type") or "") == "Anthropic":
        from ai_agent.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(config)
    return OpenAICompatibleProvider(config)


def _configured_model_capability(model=None):
    """Capability row for a model: an AI Provider Model entry with an exact
    name match wins, then the AI Settings pattern table."""
    text = str(model or "").lower()
    if text and _providers_available():
        rows = frappe.get_all(
            "AI Provider Model",
            filters={"model_name": model, "parenttype": "AI Provider"},
            fields=["model_name", "context_window_tokens", "supports_vision"],
            limit_page_length=1,
        )
        if rows:
            row = rows[0]
            return frappe._dict(
                model_pattern=row.model_name,
                context_window_tokens=row.context_window_tokens,
                supports_vision=row.supports_vision,
            )
    try:
        rows = frappe.get_cached_doc("AI Settings").get("model_capabilities") or []
    except Exception:
        return None
    for row in rows:
        pattern = str(row.get("model_pattern") or "").strip().lower()
        if pattern and pattern in text:
            return row
    return None


def estimate_context_window_tokens(model=None, base_url=None):
    """Context-window estimate: configured capability table first, then heuristics."""
    capability = _configured_model_capability(model)
    if capability and int(capability.get("context_window_tokens") or 0) > 0:
        return int(capability.get("context_window_tokens"))

    text = f"{model or ''} {base_url or ''}".lower()
    if "gemini" in text:
        return 1000000
    if "gpt-4.1" in text or "gpt-4o" in text or "deepseek" in text:
        return 128000
    if "llama-3.3" in text or "llama-3.1" in text or "mixtral" in text:
        return 128000
    if "qwen" in text:
        return 128000
    if "claude" in text:
        return 200000
    return DEFAULT_CONTEXT_WINDOW_TOKENS


def supports_vision_model(model=None, base_url=None):
    """Vision support: a matching capability row is authoritative, else heuristics."""
    capability = _configured_model_capability(model)
    if capability:
        return bool(capability.get("supports_vision"))

    text = f"{model or ''} {base_url or ''}".lower()
    if "deepseek" in text or "ollama" in text:
        return any(marker in text for marker in ("vision", "vl", "llava", "pixtral"))
    vision_markers = (
        "gpt-4o",
        "gpt-4.1",
        "o4",
        "gemini",
        "vision",
        "vl",
        "llava",
        "pixtral",
        "llama-4",
        "maverick",
        "scout",
        "qwen2.5-vl",
        "qwen-vl",
    )
    return any(marker in text for marker in vision_markers)
