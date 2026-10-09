"""Cheap, deterministic question routing before the model loop.

Keyword rules classify the request so the model receives only the tools (at
most 8) and instructions its route needs. Anything the rules cannot place is
`open_analysis`, the only route with the full exploration tool set.
"""

import re

ROUTES = ("metric", "document_lookup", "diagnose", "guide", "action", "invoice", "open_analysis")
REQUEST_MORE = "tools.request_more"

ROUTE_TOOLS = {
	"metric": ["metrics.query", REQUEST_MORE],
	"document_lookup": [
		"data.get_document", "data.get_child_records", "data.query", "erp.invoice_payments",
		"diagnose.document", "attachments.list", "metrics.query", REQUEST_MORE,
	],
	"diagnose": [
		"diagnose.document", "data.get_document", "data.get_child_records", "data.query",
		"setup.check", "knowledge.search", "schema.describe_doctype", REQUEST_MORE,
	],
	"guide": ["knowledge.search", "setup.check", "skills.load", "skills.read_reference", REQUEST_MORE],
	"action": [
		"actions.prepare_document", "actions.request_create_draft", "actions.request_mapped_draft",
		"actions.request_update", "erp.item_details", "erp.party_details", "data.query", REQUEST_MORE,
	],
	"invoice": [
		"skills.load", "skills.read_reference", "data.query",
		"actions.prepare_document", "actions.request_create_draft", "memory.search", REQUEST_MORE,
	],
	"open_analysis": None,  # every tool the agent and skills allow
}

# Maximum model-loop steps per route (the agent's own limit still applies).
ROUTE_STEP_LIMITS = {"metric": 3, "document_lookup": 4, "diagnose": 6, "guide": 4, "action": 8, "invoice": 10, "open_analysis": 10}

_ACTION = ("انشئ", "أنشئ", "انشاء", "إنشاء", "اضف", "أضف", "اضافة", "إضافة", "سجل لي", "سوي لي", "اعمل لي",
		   "حدّث", "تحديث", "عدل ", "عدّل", "تعديل", "احذف", "حذف", "الغاء", "إلغاء", "ألغ", "اعتمد")
_ACTION_EN = re.compile(r"\b(create|add|update|edit|delete|cancel|submit)\b")
_DIAGNOSE = ("لماذا لا", "ليش ما", "ليش لا", "لماذا لم", "لا يظهر", "لا تظهر", "ما يظهر", "ما تظهر", "ما ظهر",
			 "لم يظهر", "لم تظهر", "مشكلة في", "ما المشكلة", "why doesn", "why isn", "not showing", "doesn't show")
_GUIDE = ("تهيئة", "جاهزية", "إعدادات النظام", "اعدادات النظام", "ناقص في الإعداد", "setup", "كيف اعمل", "كيف أعمل", "كيف اسوي", "كيف أسوي", "كيف اضيف", "كيف أضيف", "كيف انشئ", "كيف أنشئ",
		  "طريقة عمل", "خطوات", "دليل", "شرح", "how do i", "how to")
_METRIC = ("مبيعات", "المبيعات", "مشتريات", "المشتريات", "مرتجع", "رصيد", "ارصدة", "أرصدة", "ذمم", "لنا عند",
		   "علينا", "مستحق", "استحقاق", "سيولة", "السيولة", "نقدية", "النقدية", "صندوق", "بنوك", "البنوك",
		   "ربح", "أرباح", "ارباح", "إيراد", "ايراد", "مصروف", "تحصيل", "تحصيلات", "مدفوعات", "مخزون",
		   "المخزون", "أعلى", "اعلى", "أكثر", "مندوب", "وضع الشركة", "حد إعادة الطلب", "حد اعادة الطلب",
		   "إعادة الطلب", "عدد الفواتير", "متوسط الفاتورة", "sales", "revenue", "profit", "receivable",
		   "payable", "cash", "stock", "balance")
_DEMONSTRATIVE = re.compile(r"(?:^|\s)(?:هذا|هذه|هذي|هاذا|هاذي|this)(?:\s|$|[؟?.,،])")
_DOCUMENT_REF = re.compile(r"\b[A-Z]{2,}(?:-[A-Z]{2,})*-\d{2,}(?:-\d+)*\b|\b\d{4}-\d{4,}\b")
_DOCUMENT_WORDS = ("فاتورة رقم", "سند رقم", "قيد رقم", "رقم الفاتورة", "تفاصيل الفاتورة", "آخر فاتورة",
				   "اخر فاتورة", "أحدث فاتورة", "احدث فاتورة", "آخر سند", "اخر سند", "اعطني تفاصيل", "أعطني تفاصيل", "details of")


def classify(prompt, *, has_attachments=False, has_document=False):
	text = " ".join(str(prompt or "").split())
	lowered = text.lower()
	if has_attachments:
		return "invoice"
	if _has(lowered, _DIAGNOSE):
		return "diagnose"
	if _has(lowered, _ACTION) or _ACTION_EN.search(lowered):
		return "action"
	if _DOCUMENT_REF.search(text) or _has(lowered, _DOCUMENT_WORDS):
		return "document_lookup"
	if has_document and _DEMONSTRATIVE.search(lowered):
		# "explain this", "هذه الفاتورة": about the open form, not the user guide.
		return "document_lookup"
	if _has(lowered, _GUIDE):
		return "guide"
	if _has(lowered, _METRIC):
		return "metric"
	if has_document:
		# A vague question asked on an open form is about that document.
		return "document_lookup"
	return "open_analysis"


def route_tools(route):
	return ROUTE_TOOLS.get(route)


def _has(text, needles):
	return any(needle in text for needle in needles)


def handle_request_more_stub(arguments, permission_context):
	"""tools.request_more is handled by the orchestrator (it changes the run's tool set)."""
	return {"error": "tools.request_more is only available inside an agent run."}
