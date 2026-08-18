import re

"""Zero-cost local answers for greetings and capability questions.

Matching is word/phrase-boundary based (not substring) and gated by prompt
length, so real data questions like "What is this month's balance?" or
follow-ups never get hijacked by a canned reply.
"""

ACTION_MARKERS = (
    "انش", "اضف", "سوي", "اعمل", "حدث", "عدل", "احذف", "الغ",
    "فاتور", "سند", "مورد", "عميل", "صنف", "تقرير", "كشف", "رصيد",
    "create", "add", "update", "delete", "invoice", "report",
)

GREETING_MARKERS = (
    "هلا", "اهلا", "مرحبا", "السلام", "السلام عليكم", "من انت", "مين انت",
    "who are you", "hello", "hi", "هاي",
)

CAPABILITY_MARKERS = (
    "هل تستطيع", "هل تقدر", "قدراتك", "ما تستطيع", "ماذا تستطيع",
    "المهام الكبيره", "المهام المركبه", "what can you do",
)

GREETING_MAX_CHARS = 40
CAPABILITY_MAX_CHARS = 80

GREETING_ANSWER = (
    "أهلًا 👋 أنا **الباشق**.\n\n"
    "أقدر أساعدك في التحليل، التقارير، قراءة الملفات، وتجهيز المستندات "
    "مثل الفواتير والأصناف والموردين مع طلب موافقة قبل أي حفظ."
)

CAPABILITY_ANSWER = (
    "نعم، أقدر أتعامل مع المهام الكبيرة والمركبة داخل ERP.\n\n"
    "- أجهز مستندات كبيرة مثل فواتير فيها أصناف كثيرة.\n"
    "- أبحث عن الأصناف/الموردين/العملاء الموجودين قبل إنشاء جديد.\n"
    "- أجهز المتطلبات الناقصة أولًا ثم أتابع المستند الأساسي.\n"
    "- كل عملية حفظ أو تعديل تتم عبر موافقة واضحة قبل التنفيذ."
)


def normalize_prompt(value):
    return (
        str(value or "")
        .lower()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ة", "ه")
        .replace("ى", "ي")
        .strip()
    )


def _matches_phrase(text, phrase):
    pattern = r"(?:^|[\s.,،!؟?])" + re.escape(phrase) + r"(?:$|[\s.,،!؟?])"
    return re.search(pattern, text) is not None


def greeting_or_capability_answer(prompt):
    """Return a canned answer only for short, unambiguous greeting/capability prompts."""
    text = normalize_prompt(prompt)
    if not text:
        return None
    if any(marker in text for marker in ACTION_MARKERS):
        return None
    if len(text) <= CAPABILITY_MAX_CHARS and any(
        _matches_phrase(text, marker) for marker in CAPABILITY_MARKERS
    ):
        return CAPABILITY_ANSWER
    if len(text) <= GREETING_MAX_CHARS and any(
        _matches_phrase(text, marker) for marker in GREETING_MARKERS
    ):
        return GREETING_ANSWER
    return None
