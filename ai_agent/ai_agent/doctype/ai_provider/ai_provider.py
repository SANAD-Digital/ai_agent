import frappe
from frappe import _
from frappe.model.document import Document


class AIProvider(Document):
	def validate(self):
		if self.provider_type in ("OpenAI Compatible",) and not (self.base_url or "").strip():
			frappe.throw(_("Base URL is required for an OpenAI Compatible provider."))
		if self.is_default and not self.enabled:
			frappe.throw(_("The default provider must be enabled."))
		defaults = [row for row in (self.models or []) if row.is_default]
		if len(defaults) > 1:
			frappe.throw(_("Only one model can be marked as default."))
		if not self.default_model and defaults:
			self.default_model = defaults[0].model_name
		# an embedding model fails every chat request (400 from the provider)
		bad = [name for name in [row.model_name for row in self.models or []] + [self.default_model or ""] if "embed" in (name or "").lower()]
		if bad:
			frappe.throw(_("{0} is an embedding model and cannot be used for chat. Use “Fetch Models from Provider” to pick chat models.").format(", ".join(sorted(set(bad)))))

	def on_update(self):
		if self.is_default:
			frappe.db.sql(
				"update `tabAI Provider` set is_default = 0 where name != %s and is_default = 1",
				self.name,
			)
			frappe.clear_document_cache("AI Provider", self.name)
			# AI Settings > Default Provider shows the same choice
			if frappe.db.get_single_value("AI Settings", "default_provider") != self.name:
				frappe.db.set_single_value("AI Settings", "default_provider", self.name)

	def api_key_preview(self):
		try:
			key = self.get_password("api_key") or ""
		except Exception:
			key = ""
		# Last 4 characters only: a prefix identifies the vendor/key type.
		return f"…{key[-4:]}" if len(key) > 8 else ("set" if key else "")


# Model ids that are not chat models (OpenAI and compatible servers list them all).
# What a model is for, by its name (the providers' model lists rarely say).
# First match wins. Only "Chat" models can run the assistant's chat.
MODEL_TYPES = (
	("Agent", ("antigravity", "computer-use", "robotics", "deep-research", "inkling")),
	("Embedding", ("embed",)),
	("Video", ("veo", "sora", "video")),
	("Image", ("imagen", "nano-banana", "dall-e", "gpt-image", "-image", "image-", "flux", "stable-diffusion")),
	("Speech", ("tts", "whisper", "transcribe", "speech", "audio", "-live", "realtime", "lyria", "orpheus", "playai", "kokoro", "voice", "-asr", "-stt")),
	("Other", ("moderation", "aqa", "search", "guard", "rerank")),
)
# Legacy completion-only models nobody can use any more.
NOT_LISTED = ("babbage", "davinci")


def model_type_of(name):
	low = str(name or "").lower()
	for kind, needles in MODEL_TYPES:
		if any(n in low for n in needles):
			return kind
	return "Chat"


def chat_capable(row):
	"""Whether a configured model can run the chat: a chat model that takes tools."""
	name = row.get("model_name")
	if not name:
		return False
	# The name wins over a stored "Chat": rows added before a pattern was known (a TTS model offered as chat
	# failed every message on ai.dev).
	if (row.get("model_type") or "Chat") != "Chat" or model_type_of(name) != "Chat":
		return False
	return row.get("supports_tools") in (1, True, "1", None)


@frappe.whitelist(methods=["POST"])
def fetch_models(provider):
	"""The models the provider offers this account, with the capabilities it reports.

	Returns {models: [...], stale: [configured models the provider no longer lists]}.
	Read-only: the form adds the rows the admin picks.
	"""
	doc = frappe.get_doc("AI Provider", provider)
	doc.check_permission("write")
	from ai_agent.engine.providers.factory import provider_config

	config = provider_config(doc)
	from ai_agent.engine.providers.base import is_local

	# a local server (Ollama, LM Studio…) may need no key; a cloud provider always does
	on_this_machine = is_local(config) or any(h in (config.get("base_url") or "") for h in ("localhost", "127.0.0.1"))
	if not config.get("api_key") and not on_this_machine:
		frappe.throw(_("Add the provider's API key and save, then fetch the models."))
	try:
		if config["type"] == "Anthropic":
			models = _anthropic_models(config)
		else:
			models = _openai_models(config)
	except Exception as exc:
		message = str(exc)
		if config.get("api_key"):
			message = message.replace(config["api_key"], "***")
		frappe.throw(_("Could not fetch the models from the provider: {0}").format(message[:300]))
	existing = {row.model_name for row in doc.models or []}
	for m in models:
		m["exists"] = m["model_name"] in existing
	offered = {m["model_name"] for m in models}
	stale = sorted(name for name in existing | {doc.default_model or ""} if name and name not in offered)
	return {"models": sorted(models, key=lambda m: m["model_name"]), "stale": stale}


def _anthropic_models(config):
	import anthropic

	kwargs = {"api_key": config["api_key"], "timeout": 30, "max_retries": 1}
	if config.get("base_url"):
		kwargs["base_url"] = config["base_url"]
	out = []
	for m in anthropic.Anthropic(**kwargs).models.list():   # iterating auto-paginates
		caps = getattr(m, "capabilities", None) or {}
		if hasattr(caps, "model_dump"):   # SDK 1.x returns a typed object, older ones a dict
			caps = caps.model_dump()
		thinking = caps.get("thinking") or {}
		out.append({
			"model_name": m.id,
			"label": m.display_name or m.id,
			"context_window_tokens": getattr(m, "max_input_tokens", None) or 0,
			"max_output_tokens": getattr(m, "max_tokens", None) or 0,
			"supports_vision": int(bool((caps.get("image_input") or {}).get("supported"))),
			"supports_thinking": int(bool(thinking.get("supported"))),
			"supports_pdf": int(bool((caps.get("pdf_input") or {}).get("supported"))),
			"supports_tools": 1,
			"model_type": "Chat",
		})
	return out


def _openai_models(config):
	from openai import OpenAI

	from ai_agent.engine.providers.base import guess_context_window, guess_max_output, guess_thinking, is_local

	client = OpenAI(api_key=config["api_key"] or "none", base_url=config.get("base_url") or None, timeout=30, max_retries=1)
	local = is_local(config)
	native = _gemini_native_limits(config)
	out = []
	for m in client.models.list():
		model_id = m.id
		if model_id.startswith("models/"):   # Gemini's OpenAI endpoint lists "models/<id>" but takes "<id>"
			model_id = model_id[len("models/"):]
		if any(part in model_id.lower() for part in NOT_LISTED):
			continue
		raw = m.model_dump() if hasattr(m, "model_dump") else dict(m)
		modalities = raw.get("input_modalities") or (raw.get("architecture") or {}).get("input_modalities") or []
		params = raw.get("supported_parameters") or []
		exact = native.get(model_id) or {}
		context = exact.get("context") or raw.get("context_window") or raw.get("context_length") or 0
		output = (exact.get("output") or raw.get("max_output_tokens") or raw.get("max_completion_tokens")
			or (raw.get("top_provider") or {}).get("max_completion_tokens") or 0)
		reasoning = bool(exact.get("thinking") or raw.get("effort") or raw.get("reasoning") or "reasoning" in params)
		out.append({
			"model_name": model_id,
			"label": raw.get("name") or model_id,
			"context_window_tokens": int(context or guess_context_window(model_id, local=local) or 0),
			"max_output_tokens": int(output or guess_max_output(model_id, local=local) or 0),
			"supports_vision": int("image" in modalities),
			"supports_thinking": int(reasoning or bool(guess_thinking(model_id))),
			"supports_pdf": int("file" in modalities or "pdf" in modalities),
			"supports_tools": int("tools" in params) if params else 1,
			"model_type": model_type_of(model_id),
		})
	return out


def _gemini_native_limits(config):
	"""Gemini's OpenAI-compatible model list has no limits; the native list does.

	{model id: {context, output, thinking}}; empty for other providers or on any error
	(the fetch then falls back to the family estimates).
	"""
	base = config.get("base_url") or ""
	if "generativelanguage.googleapis.com" not in base or not config.get("api_key"):
		return {}
	import requests

	try:
		response = requests.get("https://generativelanguage.googleapis.com/v1beta/models",
			params={"key": config["api_key"], "pageSize": 1000}, timeout=20)
		models = (response.json().get("models") or []) if response.ok else []
	except Exception:
		return {}
	return {
		m["name"].split("/", 1)[-1]: {"context": m.get("inputTokenLimit") or 0, "output": m.get("outputTokenLimit") or 0, "thinking": bool(m.get("thinking"))}
		for m in models if m.get("name")
	}
