"""Pure payload helpers for the purchase-invoice analysis pipeline.

Extracted verbatim from api.py: JSON extraction from model output, group and
row normalization, the canonical journal-line shape, the fixed journal
contract check, and chunk-payload merging. Nothing here touches the database
or run state.
"""

import json
import re

from frappe import _
from frappe.utils import cint, flt

MAX_ANALYSIS_ROW_TEXT_CHARS = 2400


def truncate_for_analysis(value, limit=MAX_ANALYSIS_ROW_TEXT_CHARS):
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... [truncated for chunked analysis]"


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def compact_payload_text_fields(payload):
    if isinstance(payload, dict):
        for key, value in list(payload.items()):
            if key in {"source_text", "text_preview", "raw_text", "ocr_text"} and isinstance(value, str):
                payload[key] = truncate_for_analysis(value, 4000)
            else:
                compact_payload_text_fields(value)
    elif isinstance(payload, list):
        for item in payload:
            compact_payload_text_fields(item)
    return payload


def extract_json(text):
    if not text:
        return None
    cleaned = str(text).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    if cleaned.startswith("```"):
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
        if fenced:
            fenced_cleaned = fenced.group(1).strip()
            try:
                return json.loads(fenced_cleaned)
            except Exception:
                pass

    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(cleaned):
        if char not in "{[":
            continue
        try:
            value, end = decoder.raw_decode(cleaned[index:])
        except Exception:
            continue
        candidates.append((analysis_payload_score(value), end - index, value))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]
    return None


def analysis_payload_score(value):
    if isinstance(value, dict):
        score = 10
        if any(isinstance(value.get(key), list) for key in ("invoice_groups", "document_groups", "documents", "groups")):
            score += 100
        if primary_rows(value):
            score += 80
        for group in analysis_groups(value):
            if isinstance(group, dict) and group.get("journal_entry_lines"):
                score += 120
            elif isinstance(group, dict) and primary_rows(group):
                score += 40
        if value.get("client_readable_summary"):
            score += 10
        return score
    if isinstance(value, list):
        return 50 if value else 1
    return 0


def analysis_groups(payload):
    group_keys = ["document_groups", "documents", "invoice_groups", "groups"]
    if isinstance(payload, dict) and isinstance(payload.get("invoice_groups"), list):
        return payload.get("invoice_groups") or []
    if isinstance(payload, dict):
        for key in group_keys:
            if isinstance(payload.get(key), list):
                return payload.get(key) or []
    if isinstance(payload, dict) and primary_rows(payload):
        return [payload]
    if isinstance(payload, list):
        return payload
    return []


def primary_rows(group):
    if not isinstance(group, dict):
        return []
    first_array = None
    for key in ("journal_entry_lines", "entry_lines", "accounting_lines", "lines", "extracted_lines", "observed_lines", "items"):
        if isinstance(group.get(key), list):
            if first_array is None:
                first_array = group.get(key) or []
            if group.get(key):
                return group.get(key) or []
    return first_array or []


def analysis_has_extractable_data(payload):
    groups = analysis_groups(payload)
    if not groups:
        return False
    for group in groups:
        if primary_rows(group):
            return True
        for file_info in group.get("files") or []:
            for page in file_info.get("pages") or []:
                if (
                    page.get("text_preview")
                    or page.get("metadata")
                    or page.get("totals")
                    or page.get("warnings")
                ):
                    return True
    return False


def first_analysis_error(payload):
    if isinstance(payload, dict):
        for key in ("errors", "warnings", "questions"):
            values = payload.get(key)
            if isinstance(values, list):
                for value in values:
                    text = str(value or "").strip()
                    if text:
                        return text
            elif values:
                text = str(values or "").strip()
                if text:
                    return text
    return _(
        "تعذر تحليل النص المقروء بالموديل، لذلك لم يتم إنشاء مراجعة محاسبية ناقصة."
    )


def valid_document_number(value, tax_identifier=None):
    value = str(value or "").strip()
    if not is_plausible_document_number(value):
        return ""
    if tax_identifier and value == str(tax_identifier or "").strip():
        return ""
    return value


def is_plausible_document_number(value):
    text = "".join(str(value or "").split())
    return len(text) >= 3 and any(character.isdigit() for character in text)


def normalize_analysis_payload(payload):
    if isinstance(payload, dict) and isinstance(payload.get("invoice_groups"), list):
        return payload
    if isinstance(payload, dict):
        for key in ("document_groups", "documents", "groups"):
            if isinstance(payload.get(key), list):
                normalized = dict(payload)
                normalized["invoice_groups"] = payload.get(key) or []
                return normalized
    if isinstance(payload, list):
        return {"invoice_groups": payload}
    if isinstance(payload, dict):
        return {"invoice_groups": [payload]}
    return {"invoice_groups": []}


def canonicalize_journal_payload(payload, allow_page_fallback=False):
    normalized = normalize_analysis_payload(payload)
    for index, group in enumerate(normalized.get("invoice_groups") or [], start=1):
        if not isinstance(group, dict):
            continue
        group.setdefault("invoice_group_id", f"GROUP-{index:03d}")
        rows = group.get("journal_entry_lines") if isinstance(group.get("journal_entry_lines"), list) else primary_rows(group)
        canonical_rows = []
        for line_no, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            canonical_rows.append(canonical_journal_line(row, line_no))
        group["journal_entry_lines"] = canonical_rows
    return normalized


def analysis_row_count(payload):
    count = 0
    for group in analysis_groups(normalize_analysis_payload(payload)):
        count += len(primary_rows(group))
    return count


def canonical_journal_line(row, line_no):
    canonical = dict(row)
    canonical["line_no"] = cint(row.get("line_no")) or line_no
    canonical["account"] = str(row.get("account") or "")
    canonical["account_match_method"] = str(row.get("account_match_method") or "")
    canonical["account_confidence"] = 0
    canonical["account_suggestions"] = []
    canonical["party_type"] = str(row.get("party_type") or "")
    canonical["party"] = str(row.get("party") or "")
    canonical["supplier_action"] = str(row.get("supplier_action") or "")
    canonical["debit"] = flt(row.get("debit"))
    canonical["credit"] = flt(row.get("credit"))
    canonical["description"] = str(row.get("description") or "")
    canonical["custom_cash_supplier"] = str(row.get("custom_cash_supplier") or "")
    canonical["custom_bill_no"] = str(row.get("custom_bill_no") or "")
    canonical["custom_bill_date"] = str(row.get("custom_bill_date") or "")
    canonical["custom_tax_id"] = str(row.get("custom_tax_id") or "")
    canonical["custom_tax_amount"] = flt(row.get("custom_tax_amount"))
    canonical["user_remark"] = str(row.get("user_remark") or "")
    canonical["cost_center"] = str(row.get("cost_center") or "")
    canonical["source_file"] = str(row.get("source_file") or "")
    canonical["source_page"] = cint(row.get("source_page"))
    canonical["line_meta"] = row.get("line_meta") if isinstance(row.get("line_meta"), dict) else {}
    return canonical


def model_message_content(response):
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            return message.get("content") or ""
        if response.get("content"):
            return response.get("content") or ""
    return ""


def payload_has_journal_rows(payload):
    return analysis_row_count(payload) > 0


def payload_shape(payload):
    if isinstance(payload, dict):
        return {
            "type": "dict",
            "keys": list(payload.keys())[:20],
            "group_count": len(analysis_groups(payload)),
            "row_count": analysis_row_count(payload),
        }
    if isinstance(payload, list):
        return {"type": "list", "items": len(payload), "row_count": analysis_row_count(payload)}
    return {"type": type(payload).__name__, "row_count": 0}


def group_matches_fixed_journal_contract(group):
    if not isinstance(group, dict):
        return False
    if not isinstance(group.get("document_meta"), dict):
        return False
    rows = group.get("journal_entry_lines")
    if not isinstance(rows, list) or not rows:
        return False
    required_group_meta = {
        "document_type", "document_title", "supplier_name", "supplier_tax_id",
        "document_number", "document_date", "currency", "subtotal", "tax_amount",
        "grand_total", "source_file", "source_pages",
    }
    if not required_group_meta.issubset(set(group["document_meta"].keys())):
        return False
    row_required = {
        "line_no", "account", "account_match_method", "account_confidence",
        "account_suggestions", "party_type", "party", "supplier_action", "debit",
        "credit", "description", "custom_cash_supplier", "custom_bill_no",
        "custom_bill_date", "custom_tax_id", "custom_tax_amount", "user_remark",
        "cost_center", "source_file", "source_page", "line_meta",
    }
    forbidden_row_keys = {
        "invoice_no", "invoice_number", "document_number", "document_date",
        "amount", "line_amount", "line_total", "net_amount", "total_amount",
        "tax_id", "vat_number", "tax_identifier", "supplier_name", "supplier",
        "expense_account", "debit_account", "bill_no", "bill_date",
    }
    line_meta_required = {
        "document_number", "document_date", "supplier_name", "supplier_tax_id",
        "line_amount", "tax_amount", "source_text",
    }
    for row in rows:
        if not isinstance(row, dict):
            return False
        if not row_required.issubset(set(row.keys())):
            return False
        if forbidden_row_keys.intersection(row.keys()):
            return False
        if not isinstance(row.get("line_meta"), dict):
            return False
        if not line_meta_required.issubset(set(row["line_meta"].keys())):
            return False
    return True


def merge_page_chunk_payloads(chunk_payloads):
    merged = {
        "client_readable_summary": "",
        "questions": [],
        "invoice_groups": [],
        "warnings": [],
        "errors": [],
    }
    summaries = []
    for payload in chunk_payloads or []:
        if not isinstance(payload, dict):
            continue
        if payload.get("client_readable_summary"):
            summaries.append(str(payload.get("client_readable_summary")))
        for key in ("questions", "warnings", "errors"):
            values = payload.get(key)
            if isinstance(values, list):
                merged[key].extend(values)
        for group in analysis_groups(payload):
            if isinstance(group, dict):
                merged["invoice_groups"].append(group)
        workflow = payload.get("workflow")
        if isinstance(workflow, dict) and workflow.get("analysis_failed"):
            merged.setdefault("workflow", {})["analysis_failed"] = 1
    if summaries:
        merged["client_readable_summary"] = "\n".join(summaries)
    for index, group in enumerate(merged["invoice_groups"], start=1):
        group.setdefault("invoice_group_id", f"GROUP-{index:03d}")
        group.setdefault("document_group_id", group.get("invoice_group_id"))
    return merged


def analysis_failed_payload(message, errors=None, warnings=None):
    return {
        "document_groups": [],
        "warnings": list(warnings or []),
        "errors": [message] + list(errors or []),
        "workflow": {
            "stage": "model_page_chunk_analysis",
            "analysis_failed": 1,
            "next_step": "fix_model_provider_or_ocr_and_retry",
        },
    }
