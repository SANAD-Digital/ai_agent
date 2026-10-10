from __future__ import annotations


import frappe
from frappe.utils import cint, flt


SETTINGS_DOCTYPE = "AI Chatbot Settings"
DEFAULT_AGENT_NAME = "System Chatbot"
DEFAULT_WIDGET_TITLE = "الدعم الفني"
DEFAULT_WELCOME_MESSAGE = "أهلًا! أنا مساعد النظام. اسألني عن أي مشكلة أو طلب وسأساعدك في حلها."
DEFAULT_SYSTEM_PROMPT = (
    "أنت مساعد الدعم الفني وحل المشكلات داخل نظام ERP. "
    "ساعد المستخدم في استكشاف الأخطاء وإصلاحها وحل الاستفسارات باستخدام الأدوات والمهارات المتاحة. "
    "عند الحاجة لبيانات حية استخدم الأدوات ولا تخترع أرقاماً. "
    "لا تقم بأي عملية كتابة بدون موافقة واضحة، ووضح المصدر عند الاعتماد على بيانات من النظام."
)
DEFAULT_ERROR_REPLY = (
    "عذرًا، تعذر تجهيز الرد الآن. تم تسجيل المحادثة وسيقوم فريق الدعم بمتابعتك."
)
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_TEMPERATURE = 0.7


def is_enabled() -> bool:
    return cint(_setting("enabled", 1)) == 1


def agent_name() -> str:
    value = str(_setting("agent", DEFAULT_AGENT_NAME) or "").strip()
    return value or DEFAULT_AGENT_NAME


def model() -> str | None:
    value = str(_setting("model", "") or "").strip()
    return value or None


def company() -> str | None:
    value = str(_setting("company", "") or "").strip()
    return value or None


def enable_problem_solving() -> bool:
    return cint(_setting("enable_problem_solving", 1)) == 1


def reasoning_effort() -> str:
    value = str(_setting("reasoning_effort", DEFAULT_REASONING_EFFORT) or "").strip().lower()
    if value not in {"low", "medium", "high"}:
        return DEFAULT_REASONING_EFFORT
    return value


def temperature() -> float | None:
    try:
        value = flt(_setting("temperature", DEFAULT_TEMPERATURE))
    except Exception:
        return DEFAULT_TEMPERATURE
    return min(max(value, 0.0), 1.0)


def max_history_messages() -> int:
    value = cint(_setting("max_history_messages", 8))
    return min(max(value, 0), 30)


def widget_title() -> str:
    return str(_setting("widget_title", DEFAULT_WIDGET_TITLE) or DEFAULT_WIDGET_TITLE).strip()


def welcome_message() -> str:
    return str(_setting("welcome_message", DEFAULT_WELCOME_MESSAGE) or DEFAULT_WELCOME_MESSAGE).strip()


def system_prompt() -> str:
    return str(_setting("system_prompt", DEFAULT_SYSTEM_PROMPT) or DEFAULT_SYSTEM_PROMPT).strip()


def allowed_roles() -> set[str]:
    """Roles picked in the Allowed Roles multi-select (Has Role rows on the settings)."""
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return set()
    return set(frappe.get_all(
        "Has Role",
        filters={"parenttype": SETTINGS_DOCTYPE, "parent": SETTINGS_DOCTYPE, "parentfield": "allowed_roles"},
        pluck="role",
    ))


def can_access(user=None) -> bool:
    """Whether `user` may use the support bubble (empty allowed_roles = everyone)."""
    user = user or frappe.session.user
    if user == "Administrator":
        return True
    roles = allowed_roles()
    if not roles:
        return True
    return bool(set(frappe.get_roles(user)) & roles)


def error_reply() -> str:
    return str(_setting("error_reply", DEFAULT_ERROR_REPLY) or DEFAULT_ERROR_REPLY).strip()


def _setting(field: str, default=None):
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return default
    try:
        value = frappe.db.get_single_value(SETTINGS_DOCTYPE, field)
    except Exception:
        return default
    return default if value in (None, "") else value
