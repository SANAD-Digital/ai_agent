"""Final-answer JSON contract helpers.

Pure functions extracted from the orchestrator: parsing model output into
JSON, detecting/stripping leaked tool-call markup, merging per-skill runtime
policies, and shaping the structured fallback/failure envelopes. No run
state is read or written here — callers pass the merged policy in.
"""

import json
import re

from frappe.utils import cint


def parse_json_text(content):
    text = (content or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return {}
    return {}


def parse_json_message_response(response):
    choice = response.get("choices", [{}])[0] if isinstance(response, dict) else {}
    content = (choice.get("message", {}) or {}).get("content") or ""
    return parse_json_text(content)


def extract_json_object(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        return None
    if cleaned.startswith("```"):
        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
        if match:
            cleaned = match.group(1).strip()
    # Balanced-object extraction: first-{-to-last-} corrupts answers that
    # contain two disjoint JSON snippets or trailing prose.
    decoder = json.JSONDecoder()
    index = cleaned.find("{")
    attempts = 0
    while index != -1 and attempts < 50:
        attempts += 1
        try:
            parsed, _ = decoder.raw_decode(cleaned[index:])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
        index = cleaned.find("{", index + 1)
    return None


def looks_like_tool_markup(text):
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in ("dsml", "<tool_call", "invoke name=", "parameter name=")
    )


def strip_tool_markup(text):
    text = re.sub(r"(?is)<\s*\|\s*\|\s*DSML.*?(?:calls>|$)", "", str(text or ""))
    cleaned_lines = []
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in ("dsml", "toolcalls", "invoke name=", "parameter name=")):
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def merge_skill_policies(policies):
    """Merge per-skill runtime policies into one effective policy."""
    response_format = ""
    required_output_keys = []
    instructions = []
    finalization_max_tokens = 0
    finalization_timeout = 0
    for policy in policies:
        if policy.get("response_format") == "JSON Object":
            response_format = "JSON Object"
        elif policy.get("response_format") and not response_format:
            response_format = policy.get("response_format")
        for key in policy.get("required_output_keys") or []:
            if key not in required_output_keys:
                required_output_keys.append(key)
        instruction = str(policy.get("final_output_instruction") or "").strip()
        if instruction and instruction not in instructions:
            instructions.append(instruction)
        finalization_max_tokens = max(
            finalization_max_tokens,
            cint(policy.get("finalization_max_tokens")),
        )
        finalization_timeout = max(
            finalization_timeout,
            cint(policy.get("finalization_timeout")),
        )
    return {
        "response_format": response_format,
        "required_output_keys": required_output_keys,
        "finalization_max_tokens": finalization_max_tokens,
        "finalization_timeout": finalization_timeout,
        "final_output_instruction": "\n".join(instructions),
    }


def compound_contract_pending(policy):
    """True when the active skill demands a JSON contract with substantive
    output keys — the model itself decides when that contract is ready."""
    if policy.get("response_format") != "JSON Object":
        return False
    keys = set(policy.get("required_output_keys") or [])
    return bool(keys - {"questions", "warnings", "errors", "status", "message"})


def structured_failure_reason(policy, final_answer):
    payload = extract_json_object(final_answer)
    if not isinstance(payload, dict):
        return ""
    status = str(payload.get("status") or "").strip().lower()
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    if status == "failed":
        return str(errors[0] if errors else payload.get("message") or "The structured result reported failure.")
    if not errors:
        return ""
    ignored = {"questions", "warnings", "errors", "status", "reply_markdown", "ui"}
    substantive_keys = [
        key for key in policy.get("required_output_keys") or []
        if key not in ignored
    ]
    if substantive_keys and not any(payload.get(key) for key in substantive_keys):
        return str(errors[0])
    return ""


def fallback_json_final_answer(policy, final_answer):
    fallback = {
        "status": "Needs Review",
        "client_readable_summary": str(final_answer or "")[:4000],
        "questions": [],
        "warnings": [
            "لم يرجع الوكيل بيانات منظمة صالحة بالكامل، لذلك تم حفظ حالة واضحة قابلة للمراجعة بدل رد نجاح عام."
        ],
        "errors": ["تعذر إنتاج عقد JSON النهائي المطلوب من نتائج التشغيل المتاحة."],
    }
    for key in policy.get("required_output_keys") or []:
        fallback.setdefault(key, [])
    return fallback
