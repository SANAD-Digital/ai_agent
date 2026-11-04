from __future__ import annotations

import contextlib

import frappe
from frappe.utils import cint, flt


SETTINGS_DOCTYPE = "AI Chatbot Settings"
ASSIGNMENT_DOCTYPE = "AI Chatbot Assignment"
# what a user's assignment may set (an empty field keeps the site's value)
ASSIGNED_FIELDS = ("agent", "provider", "model", "company", "reasoning_effort", "widget_title", "welcome_message",
    "system_prompt", "error_reply")
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
    """The site's switch for the support window (a user's assignment cannot turn it on when the site has it off)."""
    return cint(_setting("enabled", 1, site=True)) == 1


def agent_name() -> str:
    value = str(_setting("agent", DEFAULT_AGENT_NAME) or "").strip()
    return value or DEFAULT_AGENT_NAME


def provider() -> str | None:
    value = str(_setting("provider", "") or "").strip()
    return value or None


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
        # Not a number: the default temperature is a safe value.
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
    """Whether `user` may use the support bubble: their AI Chatbot Assignment decides when they have one (enabled is a
    grant, disabled a refusal); else one of the Allowed Roles (none picked: the AI Chatbot User role), or System
    Manager. Nobody gets it by default ("All" is gone)."""
    from ai_agent import roles as app_roles

    assignment = assignment_of(user)
    if assignment is not None:
        return cint(assignment.get("enabled")) == 1
    return app_roles.has_role(allowed_roles() or {app_roles.BUBBLE_ROLE}, user)


def assignment_of(user=None):
    """The user's AI Chatbot Assignment (enabled or not), None when they have none. Read once per request."""
    user = user or frappe.session.user
    if not user or user == "Guest" or not frappe.db.exists("DocType", ASSIGNMENT_DOCTYPE):
        return None
    cache = getattr(frappe.local, "ai_chatbot_assignments", None)
    if cache is None:
        # frappe.local is a werkzeug Local: no __dict__, attributes only
        cache = frappe.local.ai_chatbot_assignments = {}
    if user not in cache:
        cache[user] = frappe.db.get_value(ASSIGNMENT_DOCTYPE, {"user": user}, ["enabled", *ASSIGNED_FIELDS], as_dict=True)
    return cache[user]


@contextlib.contextmanager
def site_level():
    """Read the site's settings only, whoever the session user is (install and migrate configure the shared agent)."""
    before = getattr(frappe.local, "ai_chatbot_site_level", None)
    frappe.local.ai_chatbot_site_level = True
    try:
        yield
    finally:
        frappe.local.ai_chatbot_site_level = before


def error_reply() -> str:
    return str(_setting("error_reply", DEFAULT_ERROR_REPLY) or DEFAULT_ERROR_REPLY).strip()


def _setting(field: str, default=None, site=False):
    if field in ASSIGNED_FIELDS and not site and not getattr(frappe.local, "ai_chatbot_site_level", None):
        assignment = assignment_of()
        if assignment and cint(assignment.get("enabled")) and assignment.get(field) not in (None, ""):
            return assignment.get(field)
    if not frappe.db.exists("DocType", SETTINGS_DOCTYPE):
        return default
    try:
        value = frappe.db.get_single_value(SETTINGS_DOCTYPE, field)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"Chatbot setting unreadable: {field}")
        return default
    return default if value in (None, "") else value
