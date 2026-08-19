"""Arabic-facing narration for run steps and stop answers.

Pure functions extracted from the orchestrator: they turn tool calls and
results into the step titles, step summaries, and canned stop answers shown
to the user. No run state is read or written here.
"""

import json

from frappe.utils import cint

from ai_agent.runtime.result_compaction import summarize_payload


def query_spec(arguments):
    spec = arguments.get("specification") if isinstance(arguments, dict) else None
    if isinstance(spec, dict):
        return spec
    return arguments if isinstance(arguments, dict) else {}


def compact_summary(value, limit=900):
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def human_fields(fields):
    if not fields:
        return "الحقول الأساسية"
    labels = []
    for field in fields[:8]:
        if isinstance(field, dict):
            name = field.get("alias") or field.get("field") or field.get("aggregate")
            aggregate = field.get("aggregate")
            labels.append(f"{aggregate}({field.get('field')}) as {name}" if aggregate else str(name))
        else:
            labels.append(str(field))
    suffix = "..." if len(fields) > 8 else ""
    return ", ".join(labels) + suffix


def human_filters(filters):
    if not filters:
        return ""
    if isinstance(filters, dict):
        items = [f"{key}={value}" for key, value in list(filters.items())[:8]]
        suffix = "..." if len(filters) > 8 else ""
        return f" مع الفلاتر: {', '.join(items)}{suffix}"
    if isinstance(filters, list):
        items = []
        for flt in filters[:8]:
            if isinstance(flt, dict):
                items.append(
                    f"{flt.get('field')} {flt.get('operator') or '='} {flt.get('value')}"
                )
            elif isinstance(flt, (list, tuple)) and len(flt) >= 3:
                items.append(f"{flt[0]} {flt[1]} {flt[2]}")
            else:
                items.append(str(flt))
        suffix = "..." if len(filters) > 8 else ""
        return f" مع الفلاتر: {', '.join(items)}{suffix}"
    return f" مع الفلاتر: {compact_summary(filters, limit=300)}"


def tool_action_phrase(tool_name, arguments):
    if tool_name == "data.query":
        spec = query_spec(arguments)
        return f"بحثت في {spec.get('doctype') or 'السجلات'}"
    if tool_name == "data.count":
        return f"عديت سجلات {arguments.get('doctype') or 'النظام'}"
    if tool_name == "reports.run":
        return f"شغلت التقرير {arguments.get('report_name') or arguments.get('name') or arguments.get('report') or ''}".strip()
    if tool_name == "skills.load":
        return f"تحميل المهارة {arguments.get('skill_name') or ''}".strip()
    return f"تنفيذ {tool_name}"


def tool_step_title(tool_name, arguments):
    if tool_name == "data.query":
        spec = query_spec(arguments)
        doctype = spec.get("doctype") or "السجلات"
        return f"البحث في {doctype}"

    if tool_name == "data.count":
        return f"عدّ سجلات {arguments.get('doctype') or 'النظام'}"

    if tool_name == "reports.run":
        report_name = arguments.get("report_name") or arguments.get("name") or arguments.get("report") or ""
        return f"تشغيل تقرير {report_name}".strip()

    if tool_name == "reports.search":
        return "البحث عن التقارير"

    if tool_name == "reports.describe":
        return "قراءة إعداد التقرير"

    if tool_name == "schema.search_doctypes":
        return "استكشاف أنواع المستندات"

    if tool_name == "schema.describe_doctype":
        return "قراءة بنية نوع المستند"

    if tool_name == "skills.list":
        return "فحص المهارات المتاحة"

    if tool_name == "skills.load":
        return f"تحميل مهارة {arguments.get('skill_name') or ''}".strip()

    if tool_name.startswith("actions.request_"):
        return "طلب موافقة"

    if tool_name.startswith("actions.prepare_"):
        return "تجهيز مستند"

    if tool_name.startswith("actions.validate_"):
        return "التحقق من مستند"

    return f"تشغيل {tool_name}"


def summarize_data_query(arguments, result):
    spec = query_spec(arguments)
    doctype = spec.get("doctype") or "records"
    fields = human_fields(spec.get("fields") or [])
    filters = human_filters(spec.get("filters") or [])
    order = f"، الترتيب: {spec.get('order_by')}" if spec.get("order_by") else ""
    limit = f"، الحد: {spec.get('limit')}" if spec.get("limit") else ""
    rows = result.get("rows") or []
    prefix = f"بحثت في {doctype} عن {fields}{filters}{order}{limit}"
    if not rows:
        return f"{prefix}. النتيجة: لا توجد سجلات مطابقة."
    return (
        f"{prefix}. النتيجة: {result.get('row_count', len(rows))} سجلات. "
        f"أول سجل: {compact_summary(rows[0], limit=700)}"
    )


def summarize_data_count(arguments, result):
    doctype = arguments.get("doctype") or "records"
    filters = human_filters(arguments.get("filters") or [])
    return f"عديت سجلات {doctype}{filters}. النتيجة: {result.get('count', 0)}."


def summarize_tool_result(tool_name, result, arguments=None):
    arguments = arguments or {}
    if not isinstance(result, dict):
        return compact_summary(result)

    if result.get("error"):
        return f"تعذر تنفيذ {tool_action_phrase(tool_name, arguments)}: {result.get('error')}"

    if tool_name == "data.query":
        return summarize_data_query(arguments, result)

    if tool_name == "data.count":
        return summarize_data_count(arguments, result)

    if tool_name == "reports.run":
        report_name = arguments.get("report_name") or arguments.get("name") or arguments.get("report") or result.get("report", "Report")
        filters_text = human_filters(arguments.get("filters") or result.get("filters") or {})
        if result.get("is_pending") or result.get("prepared_status") in ("Queued", "Started"):
            return (
                f"شغلت التقرير {report_name}"
                f"{filters_text}. التقرير يحتاج تجهيز مسبق؛ الحالة "
                f"{result.get('prepared_status') or 'Pending'}، "
                f"Prepared Report={result.get('prepared_report_name') or 'unknown'}"
            )
        summary = result.get("summary") or {}
        numeric_fields = summary.get("numeric_fields") or []
        return (
            f"شغلت التقرير {report_name}{filters_text}. "
            f"رجع {result.get('row_count', 0)} صفوف، "
            f"والحقول الرقمية المتاحة: {', '.join(numeric_fields[:8]) or 'لا يوجد'}"
        )

    if tool_name.startswith("actions."):
        payload = result.get("payload") or {}
        payload_summary = summarize_payload(payload) if payload else {}
        parts = []
        if result.get("doctype"):
            parts.append(f"المستند={result.get('doctype')}")
        if result.get("approval_required"):
            parts.append(f"بطاقة الموافقة={result.get('approval_id')}")
        if result.get("missing_fields"):
            parts.append(f"حقول ناقصة={len(result.get('missing_fields') or [])}")
        if payload_summary:
            parts.append(
                f"حقول={payload_summary.get('field_count', 0)} "
                f"صفوف فرعية={payload_summary.get('total_child_rows', 0)}"
            )
        return "; ".join(parts) or compact_summary(result)

    if "doctypes" in result:
        names = [row.get("name") for row in result.get("doctypes", []) if row.get("name")]
        return f"وجدت {result.get('count', len(names))} DocTypes: {', '.join(names[:10]) or 'لا يوجد'}"

    if "fields" in result:
        return (
            f"{result.get('name', tool_name)}: "
            f"{len(result.get('fields', []))} حقول، "
            f"{len(result.get('links', []))} روابط، "
            f"{len(result.get('child_tables', []))} جداول فرعية."
        )

    if "rows" in result:
        rows = result.get("rows") or []
        if not rows:
            return f"{tool_action_phrase(tool_name, arguments)} ولم ترجع أي صفوف."
        return (
            f"{tool_action_phrase(tool_name, arguments)} ورجعت {result.get('row_count', len(rows))} صفوف. "
            f"أول نتيجة: {compact_summary(rows[0], limit=700)}"
        )

    if "count" in result:
        return f"{tool_action_phrase(tool_name, arguments)}. العدد: {result.get('count')}"

    if result.get("approval_required"):
        return f"تحتاج موافقة: {result.get('approval_id')}"

    return compact_summary(result)


def empty_report_loop_answer(state):
    attempts = state.get("empty_report_runs") or []
    reports = []
    for attempt in attempts:
        report = attempt.get("report")
        if report and report not in reports:
            reports.append(report)
    filters = attempts[-1].get("filters") if attempts else {}
    return (
        "لم أُكمل التحليل لأن التقارير المرشحة رجعت بدون صفوف، "
        "وأوقفت التنفيذ حتى لا يستهلك خطوات وتوكنز بتكرار نفس المحاولات.\n\n"
        f"- التقارير التي جُربت: {', '.join(reports[:5]) or 'غير محدد'}\n"
        f"- آخر فلاتر مستخدمة: `{json.dumps(filters, ensure_ascii=False, default=str)}`\n"
        "- السبب المرجح: التقرير يحتاج فلاتر مختلفة عن الأسماء التي تم تمريرها، "
        "أو لا توجد بيانات في هذا التقرير للفترة المطلوبة.\n"
        "- الخطوة الأفضل: استخدم استعلام تجميعي مباشر على بيانات القيود/الفواتير أو اختر تقريراً محدداً بفلاتره الصحيحة."
    )


def answer_from_successful_report(report_result):
    report_name = report_result.get("report") or "التقرير"
    row_count = cint(report_result.get("row_count"))
    summary = report_result.get("summary") or {}
    numeric_stats = summary.get("numeric_stats") or {}
    top_rows = summary.get("top_rows_by_numeric_field") or {}
    filters = report_result.get("filters") or {}

    lines = [
        f"وجدت بيانات في تقرير **{report_name}** لكن أوقفت التنفيذ قبل تكرار خطوات إضافية.",
        "",
        f"- عدد الصفوف: **{row_count}**",
    ]

    if filters:
        lines.append(f"- الفلاتر: `{json.dumps(filters, ensure_ascii=False, default=str)}`")

    for field, stats in list(numeric_stats.items())[:5]:
        lines.append(f"- إجمالي `{field}`: **{stats.get('total')}**")

    for field, rows in list(top_rows.items())[:2]:
        if not rows:
            continue
        lines.append("")
        lines.append(f"أعلى القيم حسب `{field}`:")
        for index, row in enumerate(rows[:5], start=1):
            label = row.get("label") or "بدون تسمية"
            lines.append(f"{index}. {label}: **{row.get('value')}**")

    lines.append("")
    lines.append("هذه إجابة مختصرة من نتيجة التقرير الناجحة لتجنب الوصول إلى حد الخطوات.")
    return "\n".join(lines)
