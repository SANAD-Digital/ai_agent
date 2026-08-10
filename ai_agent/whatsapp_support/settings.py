from __future__ import annotations

import re

import frappe
from frappe.utils import cint


DEFAULT_AGENT_NAME = "WhatsApp Technical Support"
DEFAULT_SUPPORT_USER = "Administrator"
DEFAULT_REPLY_ON_ERROR = (
    "تم استلام طلبك، لكن تعذر تجهيز رد آلي الآن. "
    "تم تسجيل الطلب وسيقوم فريق الدعم بمتابعته."
)
SETTINGS_DOCTYPE = "AI WhatsApp Support Settings"


def is_enabled() -> bool:
    return cint(_setting("enabled", _conf("ai_whatsapp_support_enabled", 1))) == 1


def agent_name() -> str:
    return str(_setting("agent", _conf("ai_whatsapp_support_agent", DEFAULT_AGENT_NAME)) or DEFAULT_AGENT_NAME).strip()


def whatsapp_device() -> str | None:
    value = str(_setting("whatsapp_device", _conf("ai_whatsapp_support_whatsapp_device", "")) or "").strip()
    return value or None


def support_user() -> str:
    user = str(_setting("support_user", _conf("ai_whatsapp_support_user", DEFAULT_SUPPORT_USER)) or DEFAULT_SUPPORT_USER).strip()
    if frappe.db.exists("User", user):
        return user
    return DEFAULT_SUPPORT_USER


def company() -> str | None:
    value = str(_setting("company", _conf("ai_whatsapp_support_company", "")) or "").strip()
    return value or None


def allow_group_chats() -> bool:
    return cint(_setting("allow_group_chats", _conf("ai_whatsapp_support_allow_groups", 0))) == 1


def allowed_phone_numbers() -> set[str]:
    raw = str(
        _setting(
            "allowed_phone_numbers",
            _conf("ai_whatsapp_support_allowed_phone_numbers", ""),
        )
        or ""
    )
    return {
        digits
        for value in re.split(r"[,;\s]+", raw)
        if 7 <= len(digits := "".join(char for char in value if char.isdigit())) <= 15
    }


def create_issue_enabled() -> bool:
    return cint(_setting("create_issue", _conf("ai_whatsapp_support_create_issue", 1))) == 1


def issue_doctype() -> str:
    return str(_setting("issue_doctype", _conf("ai_whatsapp_support_issue_doctype", "Issue")) or "Issue").strip()


def reply_on_error() -> str:
    return str(_setting("error_reply", _conf("ai_whatsapp_support_error_reply", DEFAULT_REPLY_ON_ERROR)) or DEFAULT_REPLY_ON_ERROR).strip()


def max_history_messages() -> int:
    value = cint(_setting("max_history_messages", _conf("ai_whatsapp_support_history_messages", 6)))
    return min(max(value, 0), 20)


def _conf(key: str, default=None):
    conf = getattr(frappe, "conf", {}) or {}
    return conf.get(key, default)


def _setting(field: str, default=None):
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return default
    try:
        value = frappe.db.get_single_value(SETTINGS_DOCTYPE, field)
    except Exception:
        return default
    return default if value in (None, "") else value
