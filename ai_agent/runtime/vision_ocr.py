"""Reading a scanned page with the conversation's own model, when that model sees images but takes no PDF (DeepSeek
flash, GPT-4o...): each page goes to it as an image and the texts are put together (file_context.ocr_pdf_pages).

Measured 2026-11-09 on a scanned Arabic/English invoice: deepseek-flash read the page in 5 s with clean Arabic,
where local RapidOCR took 13 s and garbled the Arabic. Its thinking is switched off for this call: left on, it
spent the whole output budget reasoning and returned no text. A model that refuses the switch is asked again
without it. Numbers still deserve a check (both readers misread one digit of the VAT on that page).
"""

from __future__ import annotations

import base64

import frappe

MAX_OUTPUT_TOKENS = 4000
_no_thinking_switch = set()  # providers whose server refused `thinking`: not offered again in this process


def reader(provider_name, model):
    """(provider, model) when the model reads images and takes no PDF itself, else None."""
    if not provider_name or not model:
        return None
    try:
        from ai_agent.engine.providers.factory import provider_for

        provider, provider_name, model = provider_for(provider_name, model)
        caps = provider.caps(model)
    except Exception:
        return None  # no key, provider disabled or not allowed: the local OCR reads
    if not caps.supports_vision or caps.supports_pdf or provider.name != "openai":
        return None
    return (provider_name, model)


def reader_of_run(run_name):
    if not run_name:
        return None
    run = frappe.db.get_value("AI Run", run_name, ["provider", "model"], as_dict=True) or {}
    return reader(run.get("provider"), run.get("model"))


def tag(vision):
    return f"{vision[0]}/{vision[1]}" if vision else "ocr"


def read_image(vision, data, mime_type="image/png", prompt=""):
    """The text of one image, read by the model; "" when it returns nothing."""
    from openai import BadRequestError

    from ai_agent.engine.providers.factory import provider_for

    provider, provider_name, model = provider_for(*vision)
    kwargs = {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt or "Extract all visible text from this image exactly. Return plain text only."},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(data).decode()}"}},
        ]}],
    }
    if provider_name not in _no_thinking_switch:
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    try:
        response = provider.client().chat.completions.create(**kwargs)
    except BadRequestError:
        if "extra_body" not in kwargs:
            raise
        _no_thinking_switch.add(provider_name)
        kwargs.pop("extra_body")
        response = provider.client().chat.completions.create(**kwargs)
    return ((response.choices[0].message.content if response.choices else "") or "").strip()
