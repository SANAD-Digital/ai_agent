"""Errors as the user should read them: what happened, why, and what to do next. Never a raw tool error."""

import re

RULES = [
    # (pattern, kind, title, reason template, suggestion, retry)
    (r"No (read|create|write|submit|cancel|delete) permission on (?:DocType )?'?([^'.]+)'?", "permission", "لا تملك الصلاحية اللازمة",
     "حسابك لا يملك صلاحية {verb} على «{target}».", "اطلب من مدير النظام منحك هذه الصلاحية ثم أعد المحاولة.", False),
    (r"fiscal year", "validation", "السنة المالية غير محددة",
     "تعذّر تنفيذ التقرير لأن السنة المالية أو الفترة غير محددة.", "حدّد السنة المالية أو تاريخي البداية والنهاية ثم أعد المحاولة.", True),
    (r"not (found or not )?accessible|does not exist|not found", "not_found", "المستند غير متاح",
     "المستند المطلوب غير موجود، أو لا تملك صلاحية عرضه.", "تحقق من رقم المستند أو ابحث عنه بالاسم.", False),
    (r"missing required argument\(s\): (.+?)\.?$|([a-z_, ]+) (?:is|are) required", "validation", "بيانات ناقصة",
     "الطلب ينقصه: {target}.", "أكمل البيانات الناقصة ثم أعد المحاولة.", True),
    (r"high cost|requires additional filters|add more filters", "validation", "النتيجة أكبر من اللازم",
     "الاستعلام واسع جدًا ولا يمكن تنفيذه كما هو.", "حدّد فترة زمنية أو شركة أو طرفًا لتضييق النتيجة.", True),
    (r"call limit", "limit", "بلغت الحد المسموح", "تم بلوغ الحد الأقصى لعدد مرات استخدام هذه الأداة في هذه المهمة.", "ابدأ طلبًا جديدًا.", False),
    (r"rate limit", "limit", "طلبات كثيرة في وقت قصير", "تجاوزت عدد الطلبات المسموح في الدقيقة.", "انتظر دقيقة ثم أعد المحاولة.", True),
    (r"system configuration; it cannot be created or changed", "policy", "إعداد نظام لا يُعدَّل من المساعد",
     "«{target}» من إعدادات النظام، وتعديله متاح من ERPNext فقط.", "افتح الإعداد في ERPNext أو اطلب ذلك من مدير النظام.", False),
    (r"not allowed by this agent|is not available|requires approval", "policy", "الإجراء غير متاح",
     "هذا الإجراء غير مفعّل في إعدادات المساعد الحالية.", "اطلب من مدير النظام تفعيله إن كان مطلوبًا.", False),
    (r"Tool execution failed|Internal server error", "failure", "حدث خطأ غير متوقع",
     "تعذّر إكمال العملية بسبب خطأ داخلي، وقد سُجّل لدى مدير النظام.", "أعد المحاولة، وإن تكرر الخطأ فأبلغ مدير النظام.", True),
]
VERBS = {"read": "القراءة", "create": "الإنشاء", "write": "التعديل", "submit": "الاعتماد", "cancel": "الإلغاء", "delete": "الحذف"}
ARABIC = re.compile(r"[\u0600-\u06FF]")


def friendly(message, tool=None):
    """{kind, title, reason, suggestion, retry, raw} for any error text."""
    from ai_agent.presentation.labels import doctype_label

    raw = str(message or "").strip()
    for pattern, kind, title, reason, suggestion, retry in RULES:
        match = re.search(pattern, raw, re.IGNORECASE)
        if not match:
            continue
        groups = [g for g in match.groups() if g]
        target = groups[-1].strip() if groups else ""
        verb = VERBS.get(groups[0].lower(), "") if kind == "permission" and groups else ""
        if kind in ("permission", "policy") and target:
            target = doctype_label(target) if target[:1].isupper() else target
        if kind == "policy" and "{target}" in reason and not target:
            target = raw.split(" is system configuration")[0].strip()
        return {"kind": kind, "title": title, "reason": reason.format(target=target or "—", verb=verb), "suggestion": suggestion, "retry": retry, "raw": raw}
    if ARABIC.search(raw):      # already written for the user
        return {"kind": "failure", "title": "تعذّر تنفيذ الطلب", "reason": raw, "suggestion": None, "retry": False, "raw": raw}
    return {"kind": "failure", "title": "تعذّر تنفيذ الطلب", "reason": "لم تكتمل العملية.", "suggestion": "أعد المحاولة، وإن تكرر الخطأ فأبلغ مدير النظام.",
            "retry": True, "raw": raw}


def text_of(view):
    return " ".join(x for x in (view["title"] + ":", view["reason"], view.get("suggestion")) if x)
