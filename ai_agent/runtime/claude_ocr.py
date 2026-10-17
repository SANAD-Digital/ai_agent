"""Reading scanned PDFs and images with Claude, through the site's Anthropic provider.

The OCR chain was Gemini (blocked in Managed mode: data may not leave to Google) and
local RapidOCR (often not installed), so a scanned attachment came back empty. Claude
reads PDFs (each page as text and image) and images natively, and an Anthropic
provider is the one Managed mode always allows. A whole PDF goes in one request; the
caller caches the text (FileContextService, 24 h), so a file is read once.

The key comes from the AI Provider record and never appears in errors or logs.
"""

import base64

import frappe

MAX_PDF_BYTES = 30 * 1024 * 1024   # the Messages API takes requests up to 32 MB
MAX_OUTPUT_TOKENS = 16000          # non-streaming: stays well inside the SDK's request timeout


def _provider():
	"""(api_key, base_url, model) of the enabled Anthropic provider with a key, default first; else None."""
	from ai_agent.engine.providers.factory import anthropic_base_url

	for row in frappe.get_all("AI Provider", filters={"enabled": 1, "provider_type": "Anthropic"},
			fields=["name", "base_url", "default_model"], order_by="is_default desc, modified desc"):
		doc = frappe.get_doc("AI Provider", row.name)
		try:
			key = doc.get_password("api_key") if doc.get("api_key") else ""
		except Exception:
			key = ""
		if key:
			model = row.default_model or next((m.model_name for m in doc.models if m.is_default), None) or "claude-opus-5-5"
			return key, anthropic_base_url(row.base_url) or None, model
	return None


def available():
	return _provider() is not None


def _read(content, prompt):
	import anthropic

	found = _provider()
	if not found:
		return ""
	key, base_url, model = found
	client = anthropic.Anthropic(api_key=key, base_url=base_url, timeout=300, max_retries=1)
	try:
		response = client.messages.create(
			model=model,
			max_tokens=MAX_OUTPUT_TOKENS,
			output_config={"effort": "low"},   # transcription, not reasoning
			messages=[{"role": "user", "content": [*content, {"type": "text", "text": prompt}]}],
		)
	except anthropic.APIError as exc:
		message = str(getattr(exc, "message", "") or exc).replace(key, "***")
		frappe.log_error(message[:1000], "AI Claude OCR")
		return ""
	if response.stop_reason == "refusal":
		return ""
	return "\n".join(block.text for block in response.content if block.type == "text").strip()


def read_pdf(path, prompt):
	with open(path, "rb") as f:
		data = f.read(MAX_PDF_BYTES + 1)
	if len(data) > MAX_PDF_BYTES:
		return ""
	return _read([{"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
		"data": base64.standard_b64encode(data).decode("ascii")}}], prompt)


def read_image(data, mime_type, prompt):
	if mime_type not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
		return ""
	return _read([{"type": "image", "source": {"type": "base64", "media_type": mime_type,
		"data": base64.standard_b64encode(data).decode("ascii")}}], prompt)
