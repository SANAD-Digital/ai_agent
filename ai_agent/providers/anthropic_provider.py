"""Native Anthropic (Claude) provider on the official `anthropic` SDK.

The orchestrator speaks the OpenAI chat shape (messages with tool_calls, tool
messages, choices[0].message). This provider translates that shape to the
Messages API and back, so the rest of the runtime is unchanged.

Prompt caching: the static part of the system prompt (everything before
``DYNAMIC_CONTEXT_MARKER``) and the tool definitions carry cache_control;
per-run context (user, date, roles, session checkpoint) comes after the last
breakpoint so it never invalidates the cached prefix.

The API key is read from the provider's Password field and is never logged,
returned or included in error messages.
"""

import base64
import json

import frappe

from ai_agent.runtime.exceptions import MYSYSAgentError

DYNAMIC_CONTEXT_MARKER = "Per-run context:"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_MAX_TOKENS = 16000
CACHE = {"type": "ephemeral"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class AnthropicProviderError(MYSYSAgentError):
	pass


class AnthropicProvider:
	provider_type = "Anthropic"

	def __init__(self, settings):
		self.settings = settings
		self.model = settings.get("default_model") or DEFAULT_MODEL
		self.base_url = (settings.get("base_url") or "").strip() or None
		self.timeout = float(settings.get("request_timeout") or 120)
		self._client = None

	# -- client ---------------------------------------------------------------

	def _get_client(self):
		if self._client is None:
			import anthropic

			api_key = self.settings.get_password("api_key") if hasattr(self.settings, "get_password") else None
			if not api_key:
				raise AnthropicProviderError("The Anthropic provider has no API key configured.")
			kwargs = {"api_key": api_key, "timeout": self.timeout, "max_retries": 2}
			if self.base_url:
				kwargs["base_url"] = self.base_url
			self._client = anthropic.Anthropic(**kwargs)
		return self._client

	# -- public interface (same as OpenAICompatibleProvider) ------------------

	def create_step(self, *, instructions, input_items, tools, state):
		request = self.build_request(instructions, tools)
		try:
			with self._get_client().messages.stream(**request) as stream:
				message = stream.get_final_message()
		except Exception as exc:
			raise AnthropicProviderError(_safe_error(exc)) from None
		return to_openai_response(message)

	def create_step_stream(self, *, instructions, input_items, tools, state):
		"""Yield the accumulated text as it grows; return the final text."""
		request = self.build_request(instructions, tools)
		parts = []
		try:
			with self._get_client().messages.stream(**request) as stream:
				for text in stream.text_stream:
					if text:
						parts.append(text)
						yield "".join(parts)
		except Exception as exc:
			raise AnthropicProviderError(_safe_error(exc)) from None
		return "".join(parts)

	def continue_step(self, *, response_id, tool_outputs, tools, state):
		raise AnthropicProviderError("continue_step is not used with the Messages API.")

	def get_usage(self, response):
		usage = (response or {}).get("usage") or {}
		return {
			"input_tokens": int(usage.get("prompt_tokens") or 0),
			"output_tokens": int(usage.get("completion_tokens") or 0),
			"cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
			"cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
		}

	def get_context_window(self, model=None):
		from ai_agent.runtime.model_provider import estimate_context_window_tokens

		return estimate_context_window_tokens(model or self.model, self.base_url or "")

	# -- request translation ---------------------------------------------------

	def build_request(self, instructions, tools):
		model = instructions.get("model") or self.model
		system_blocks, messages = convert_messages(instructions.get("messages") or [])
		if instructions.get("response_format"):
			system_blocks.append({"type": "text", "text": "Return only one valid JSON object, with no text around it."})
		request = {
			"model": model,
			"max_tokens": int(instructions.get("max_tokens") or DEFAULT_MAX_TOKENS),
			"messages": messages,
		}
		if system_blocks:
			request["system"] = system_blocks
		if tools:
			request["tools"] = convert_tools(tools)
		effort = str(instructions.get("reasoning_effort") or self.settings.get("default_reasoning_effort") or "").lower()
		if effort in EFFORTS:
			request["output_config"] = {"effort": effort}
		return request


def convert_tools(tools):
	"""OpenAI-style {name, description, parameters} -> Messages API tools.

	The tool list is deterministic, so a breakpoint on the last tool caches
	all tool definitions together with the static system prompt.
	"""
	converted = [
		{
			"name": tool["name"],
			"description": tool.get("description") or "",
			"input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
		}
		for tool in tools
	]
	if converted:
		converted[-1]["cache_control"] = CACHE
	return converted


def convert_messages(messages):
	"""Split OpenAI-shaped messages into (system blocks, Messages API turns)."""
	system_blocks = []
	turns = []

	def add(role, blocks):
		if turns and turns[-1]["role"] == role:
			turns[-1]["content"].extend(blocks)
		else:
			turns.append({"role": role, "content": list(blocks)})

	for index, message in enumerate(messages):
		role = message.get("role")
		content = message.get("content")
		if role == "system":
			if not turns:
				system_blocks.extend(_system_blocks(_text(content)))
			else:
				# Later operator instructions (e.g. finalization) ride on the next user turn.
				add("user", [{"type": "text", "text": f"[Operator instruction]\n{_text(content)}"}])
		elif role == "user":
			add("user", _user_blocks(content))
		elif role == "assistant":
			blocks = message.get("provider_blocks")
			if not blocks:
				blocks = []
				if _text(content).strip():
					blocks.append({"type": "text", "text": _text(content)})
				for call in message.get("tool_calls") or []:
					function = call.get("function") or {}
					try:
						arguments = json.loads(function.get("arguments") or "{}")
					except (TypeError, ValueError):
						arguments = {}
					blocks.append({
						"type": "tool_use",
						"id": call.get("id"),
						"name": function.get("name"),
						"input": arguments if isinstance(arguments, dict) else {},
					})
			if blocks:
				add("assistant", blocks)
		elif role == "tool":
			add("user", [{
				"type": "tool_result",
				"tool_use_id": message.get("tool_call_id"),
				"content": _text(content),
			}])
	if turns and turns[0]["role"] != "user":
		turns.insert(0, {"role": "user", "content": [{"type": "text", "text": "Continue."}]})
	return system_blocks, turns


def _system_blocks(text):
	static, marker, dynamic = text.partition(DYNAMIC_CONTEXT_MARKER)
	blocks = []
	if static.strip():
		blocks.append({"type": "text", "text": static.rstrip(), "cache_control": CACHE})
	if marker and dynamic.strip():
		blocks.append({"type": "text", "text": f"{marker}{dynamic}"})
	return blocks


def _user_blocks(content):
	if isinstance(content, list):
		blocks = []
		for part in content:
			if not isinstance(part, dict):
				continue
			if part.get("type") == "text":
				blocks.append({"type": "text", "text": part.get("text") or ""})
			elif part.get("type") == "image_url":
				block = _media_block((part.get("image_url") or {}).get("url") or "")
				if block:
					blocks.append(block)
		return blocks or [{"type": "text", "text": "(empty)"}]
	return [{"type": "text", "text": _text(content) or "(empty)"}]


def _media_block(url):
	"""data: URLs become image blocks, or document blocks for PDFs (no external OCR needed)."""
	if not url.startswith("data:") or ";base64," not in url:
		return {"type": "image", "source": {"type": "url", "url": url}} if url.startswith("http") else None
	header, data = url[5:].split(";base64,", 1)
	media_type = header or "image/png"
	data = "".join(data.split())
	try:
		base64.b64decode(data[:64] + "=" * (-len(data[:64]) % 4))
	except Exception:
		return None
	kind = "document" if media_type == "application/pdf" else "image"
	return {"type": kind, "source": {"type": "base64", "media_type": media_type, "data": data}}


def pdf_document_block(content_bytes):
	"""A PDF as a document block, for reading invoices directly without OCR."""
	return {
		"type": "document",
		"source": {
			"type": "base64",
			"media_type": "application/pdf",
			"data": base64.standard_b64encode(content_bytes).decode("ascii"),
		},
	}


def _text(content):
	if content is None:
		return ""
	if isinstance(content, str):
		return content
	if isinstance(content, list):
		return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
	return json.dumps(content, ensure_ascii=False, default=str)


# -- response translation ------------------------------------------------------


def to_openai_response(message):
	"""Messages API message -> the OpenAI chat shape the orchestrator reads."""
	data = message.to_dict() if hasattr(message, "to_dict") else dict(message)
	blocks = data.get("content") or []
	text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
	tool_calls = [
		{
			"id": block.get("id"),
			"type": "function",
			"function": {
				"name": block.get("name"),
				"arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
			},
		}
		for block in blocks
		if block.get("type") == "tool_use"
	]
	stop_reason = data.get("stop_reason")
	finish_reason = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(stop_reason, stop_reason)
	message_out = {"role": "assistant", "content": text or None}
	if tool_calls:
		message_out["tool_calls"] = tool_calls
		# Thinking blocks must be replayed unchanged with the tool results.
		message_out["provider_blocks"] = blocks
	if stop_reason == "refusal":
		message_out["content"] = text or "لا أستطيع المساعدة في هذا الطلب."
	usage = data.get("usage") or {}
	fresh = int(usage.get("input_tokens") or 0)
	created = int(usage.get("cache_creation_input_tokens") or 0)
	read = int(usage.get("cache_read_input_tokens") or 0)
	return {
		"id": data.get("id"),
		"model": data.get("model"),
		"choices": [{"index": 0, "message": message_out, "finish_reason": finish_reason}],
		"usage": {
			# prompt_tokens = every input token, cached or not, like OpenAI's field.
			"prompt_tokens": fresh + created + read,
			"completion_tokens": int(usage.get("output_tokens") or 0),
			"input_tokens": fresh,
			"cache_creation_input_tokens": created,
			"cache_read_input_tokens": read,
			"prompt_tokens_details": {"cached_tokens": read},
		},
	}


def _safe_error(exc):
	"""Status and API message only: never headers, request bodies or the key."""
	try:
		import anthropic
	except ImportError:
		return "Anthropic SDK is not installed."
	if isinstance(exc, anthropic.APIStatusError):
		detail = ""
		try:
			detail = (exc.body or {}).get("error", {}).get("message", "")
		except Exception:
			detail = ""
		return f"Anthropic API error {exc.status_code}: {detail or type(exc).__name__}"[:500]
	if isinstance(exc, anthropic.APIConnectionError):
		return "Could not reach the Anthropic API."
	if isinstance(exc, AnthropicProviderError):
		return str(exc)
	frappe.log_error(title="Anthropic Provider Error", message=type(exc).__name__)
	return f"Anthropic provider error: {type(exc).__name__}"
