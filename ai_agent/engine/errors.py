"""User-facing wording for failed replies.

Provider and engine errors are technical (HTTP codes, JSON bodies, model ids).
Users see one short sentence in their language that says what to do; the raw
text is kept on the AI Run for administrators.
"""

import frappe

MESSAGES = {
	"rate_limit": "The AI service is busy or the usage limit was reached. Try again in a minute.",
	"quota": "The AI provider account has no remaining credit or quota. Ask your administrator.",
	"auth": "The AI service rejected the connection. Ask your administrator to check the provider's API key.",
	"model": "The selected model is not available. Choose another model and try again.",
	"no_tools": "The selected model cannot use the system's tools. Choose another model.",
	"context_overflow": "This conversation is too long for the model. Start a new conversation.",
	"timeout": "The AI service took too long to answer. Try again.",
	"server": "The AI service is having a problem right now. Try again shortly.",
	"interrupted": "The reply was interrupted. Try again.",
	"setup": "The assistant is not set up correctly for this request. Ask your administrator.",
	"limit": "The request needed more steps than allowed. Try asking for a smaller part.",
	"empty": "The model returned an empty answer. Try again.",
	"error": "The reply could not be completed. Try again, or choose another model.",
}

# Raw provider text that identifies a cause more precisely than the HTTP class.
PATTERNS = (
	("no_tools", ("function calling is not enabled", "does not support tools", "tools are not supported", "tool use is not supported")),
	("quota", ("insufficient_quota", "exceeded your current quota", "insufficient balance", "credit balance", "billing")),
	("model", ("model not found", "is not found for api version", "does not exist", "unknown model", "model_not_found", "no such model")),
)


def kind_of(kind, raw):
	low = str(raw or "").lower()
	for refined, needles in PATTERNS:
		if any(n in low for n in needles):
			return refined
	if kind not in MESSAGES or kind == "error":
		from ai_agent.engine.providers.base import classify_error

		kind = classify_error(Exception(raw or ""))[0]
	return kind if kind in MESSAGES else "error"


def is_user_message(text):
	"""True when `text` is already one of these sentences (in any language the site uses)."""
	if not text:
		return False
	langs = {"en", frappe.local.lang or "en", frappe.db.get_default("lang") or "en"}
	return any(text in (msg, frappe._(msg, lang=lang)) for msg in MESSAGES.values() for lang in langs)


def user_message(kind, raw="", user=None):
	"""One translated sentence for the message owner."""
	lang = frappe.db.get_value("User", user, "language") if user else None
	return frappe._(MESSAGES[kind_of(kind, raw)], lang=lang or frappe.local.lang)
