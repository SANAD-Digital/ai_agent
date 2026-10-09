"""Business vocabulary: Arabic labels, status badges and field priority tiers.

Everything here is server-defined so the same field always reads and ranks the
same way, whatever the LLM or the DocType field order happens to be.
"""

import re

# Tier 1 primary, 2 operational, 3 secondary, 4 technical (hidden by default).
TIER_TECHNICAL = 4

FIELD_LABELS = {
    "name": "المستند",
    "customer": "العميل",
    "customer_name": "العميل",
    "supplier": "المورد",
    "supplier_name": "المورد",
    "party": "الطرف",
    "party_name": "الطرف",
    "company": "الشركة",
    "company_name": "الشركة",
    "default_currency": "العملة",
    "currency": "العملة",
    "account_currency": "العملة",
    "grand_total": "الإجمالي",
    "base_grand_total": "الإجمالي (عملة الشركة)",
    "rounded_total": "الإجمالي المقرّب",
    "total": "مجموع الأصناف",
    "net_total": "الصافي قبل الضريبة",
    "outstanding_amount": "المستحق",
    "total_advance": "الدفعات المقدمة",
    "discount_amount": "الخصم",
    "conversion_rate": "سعر الصرف",
    "stock_qty": "الكمية بوحدة المخزون",
    "uom": "الوحدة",
    "stock_uom": "وحدة المخزون",
    "conversion_factor": "معامل التحويل",
    "total_qty": "إجمالي الكمية",
    "update_stock": "تحديث المخزون",
    "set_warehouse": "المخزن",
    "debit_to": "حساب المدينين",
    "credit_to": "حساب الدائنين",
    "total_debit": "إجمالي المدين",
    "total_credit": "إجمالي الدائن",
    "received_amount": "المبلغ المستلم",
    "paid_from": "مدفوع من حساب",
    "paid_to": "مدفوع إلى حساب",
    "payment_type": "نوع السند",
    "reference_no": "رقم المرجع",
    "reference_date": "تاريخ المرجع",
    "tax_amount": "مبلغ الضريبة",
    "account_head": "حساب الضريبة",
    "description": "الوصف",
    "allocated_amount": "المبلغ المخصص",
    "reference_name": "المستند المرجعي",
    "reference_doctype": "نوع المرجع",
    "base_amount": "المبلغ (عملة الشركة)",
    "net_amount": "الصافي",
    "is_return": "مرتجع",
    "is_pos": "نقطة بيع",
    "is_opening": "قيد افتتاحي",
    "tax_id": "الرقم الضريبي",
    "payment_terms_template": "شروط الدفع",
    "taxes_and_charges": "قالب الضرائب",
    "tax_category": "فئة الضريبة",
    "total_taxes_and_charges": "الضرائب والرسوم",
    "selling_price_list": "قائمة الأسعار",
    "buying_price_list": "قائمة الأسعار",
    "against_income_account": "حساب الإيراد",
    "bill_no": "رقم فاتورة المورد",
    "bill_date": "تاريخ فاتورة المورد",
    "workflow_state": "حالة سير العمل",
    "item_group": "مجموعة الأصناف",
    "actual_qty": "المتوفر بالمخزن",
    "paid_amount": "المدفوع",
    "amount": "المبلغ",
    "rate": "السعر",
    "qty": "الكمية",
    "projected_qty": "الكمية المتوقعة",
    "reserved_qty": "المحجوز",
    "stock_value": "قيمة المخزون",
    "valuation_rate": "سعر التقييم",
    "item_code": "الصنف",
    "item_name": "اسم الصنف",
    "warehouse": "المخزن",
    "posting_date": "التاريخ",
    "transaction_date": "التاريخ",
    "due_date": "الاستحقاق",
    "status": "الحالة",
    "debit": "مدين",
    "credit": "دائن",
    "balance": "الرصيد",
    "account": "الحساب",
    "voucher_type": "النوع",
    "voucher_no": "المستند",
    "remarks": "البيان",
    "against": "مقابل",
    "cost_center": "مركز التكلفة",
    "branch": "الفرع",
    "project": "المشروع",
    "territory": "المنطقة",
    "customer_group": "مجموعة العملاء",
    "supplier_group": "مجموعة الموردين",
    "sales_person": "مندوب المبيعات",
    "mode_of_payment": "طريقة الدفع",
    "total_number_of_shares": "الأسهم",
    "country": "الدولة",
    "abbr": "الاختصار",
    "domain": "النشاط",
    "date_of_establishment": "تاريخ التأسيس",
    "capital": "رأس المال",
    "paid_up_capital": "رأس المال المدفوع",
    "value": "القيمة",
    "total_sales": "المبيعات",
    "previous": "السابق",
    "change": "التغير",
    "percentage": "النسبة",
    "share": "النسبة",
    "count": "العدد",
    "invoices": "الفواتير",
    "in_words": "المبلغ كتابة",
    "posting_time": "الوقت",
    "creation": "تاريخ الإنشاء",
    "modified": "آخر تعديل",
    "owner": "المنشئ",
}

DOCTYPE_LABELS = {
    "Sales Invoice": "فاتورة بيع",
    "Purchase Invoice": "فاتورة شراء",
    "Payment Entry": "سند دفع",
    "Journal Entry": "قيد يومية",
    "Sales Order": "أمر بيع",
    "Purchase Order": "أمر شراء",
    "Delivery Note": "إذن تسليم",
    "Purchase Receipt": "إيصال استلام",
    "Stock Entry": "حركة مخزون",
    "Quotation": "عرض سعر",
    "Customer": "عميل",
    "Supplier": "مورد",
    "Item": "صنف",
    "Company": "شركة",
    "Warehouse": "مخزن",
    "GL Entry": "قيد أستاذ",
    "Stock Ledger Entry": "حركة مخزون",
    "Material Request": "طلب مواد",
}

DOCTYPE_PLURALS = {
    "Sales Invoice": "فواتير البيع",
    "Purchase Invoice": "فواتير الشراء",
    "Payment Entry": "سندات الدفع",
    "Journal Entry": "قيود اليومية",
    "Customer": "العملاء",
    "Supplier": "الموردون",
    "Item": "الأصناف",
    "Company": "الشركات",
    "Warehouse": "المخازن",
    "GL Entry": "قيود الأستاذ",
    "Stock Ledger Entry": "حركات المخزون",
}

# (label, tone). Tones are semantic only: success warning danger info neutral.
STATUS = {
    "draft": ("مسودة", "neutral"),
    "submitted": ("معتمدة", "info"),
    "paid": ("مدفوعة", "success"),
    "partly paid": ("مدفوعة جزئيًا", "warning"),
    "unpaid": ("غير مدفوعة", "warning"),
    "overdue": ("متأخرة", "danger"),
    "cancelled": ("ملغاة", "neutral"),
    "return": ("مرتجع", "neutral"),
    "credit note issued": ("إشعار دائن", "neutral"),
    "completed": ("مكتملة", "success"),
    "to deliver and bill": ("للتسليم والفوترة", "info"),
    "to bill": ("للفوترة", "info"),
    "to deliver": ("للتسليم", "info"),
    "closed": ("مغلقة", "neutral"),
    "open": ("مفتوحة", "info"),
    "pending": ("بانتظار الموافقة", "warning"),
    "approved": ("تمت الموافقة", "success"),
    "rejected": ("مرفوضة", "danger"),
    "executed": ("تم التنفيذ", "success"),
    "expired": ("منتهية", "neutral"),
    "failed": ("فشلت", "danger"),
    "enabled": ("مفعّل", "success"),
    "disabled": ("معطّل", "neutral"),
    "active": ("نشط", "success"),
    "inactive": ("غير نشط", "neutral"),
}

MONEY_HINTS = (
    "amount", "total", "balance", "debit", "credit", "outstanding", "paid",
    "capital", "revenue", "sales", "cost", "price", "value", "tax", "charges",
    "rate_amount", "sum", "profit", "expense", "income",
)
NOT_MONEY = ("qty", "count", "rate", "percent", "number_of", "days", "idx")
DATE_FIELDS = re.compile(r"(^|_)(date|creation|modified)$|^posting_date$")

TIER1 = {
    "name", "customer", "customer_name", "supplier", "supplier_name", "party", "party_name",
    "company", "company_name", "grand_total", "base_grand_total", "rounded_total", "total",
    "status", "posting_date", "transaction_date", "item_code", "item_name", "account",
    "voucher_no", "debit", "credit", "balance", "actual_qty", "stock_value", "capital",
    "paid_up_capital", "default_currency", "currency", "value", "amount",
}
TIER2 = {
    "warehouse", "branch", "cost_center", "mode_of_payment", "due_date", "outstanding_amount",
    "paid_amount", "net_total", "voucher_type", "qty", "rate", "valuation_rate", "territory",
    "customer_group", "supplier_group", "total_taxes_and_charges", "account_currency",
    "projected_qty", "reserved_qty", "country", "abbr", "against",
}
TIER3 = {"remarks", "sales_person", "project", "domain", "in_words", "posting_time", "po_no",
         "reference", "reference_no", "reference_date", "date_of_establishment"}
TECHNICAL = {
    "owner", "creation", "modified", "modified_by", "docstatus", "idx", "parent",
    "parenttype", "parentfield", "doctype", "naming_series", "amended_from", "_user_tags",
    "_comments", "_assign", "_liked_by", "lft", "rgt", "old_parent", "is_group_row",
}


# Reading order inside a tier: who/what first, then when, then amounts, status last.
PRIORITY_ORDER = [
    "name", "voucher_no", "customer_name", "customer", "supplier_name", "supplier", "party_name", "party", "company",
    "item_code", "item_name", "account", "posting_date", "transaction_date", "default_currency", "currency", "due_date",
    "warehouse", "voucher_type", "mode_of_payment", "actual_qty", "qty", "rate", "valuation_rate", "debit", "credit",
    "grand_total", "total", "net_total", "paid_amount", "outstanding_amount", "balance", "stock_value", "capital", "status",
]


def humanize(fieldname):
    return str(fieldname or "").replace("_", " ").strip().title()


def field_tier(fieldname):
    key = str(fieldname or "")
    if key in TECHNICAL or key.startswith("_") or key.endswith("_hash"):
        return TECHNICAL_TIER
    if key in TIER1:
        return 1
    if key in TIER2:
        return 2
    if key in TIER3:
        return 3
    # Unknown fields sit between operational and secondary so they still show.
    return 3


TECHNICAL_TIER = TIER_TECHNICAL


def label_language():
    """Labels follow the language of the signed-in user (API sessions run in English by default)."""
    try:
        import frappe

        user = frappe.session.user
        cache = frappe.local.__dict__.setdefault("_ai_label_lang", {})
        if user not in cache:
            cache[user] = frappe.db.get_value("User", user, "language") or "ar"
        return cache[user]
    except Exception:
        return "ar"


def translate(text):
    try:
        import frappe

        return frappe._(text, lang=label_language())
    except Exception:
        return text


def label_for(fieldname, doctype=None):
    if label_language() == "ar" and fieldname in FIELD_LABELS:
        return FIELD_LABELS[fieldname]
    base = str(fieldname or "")
    if base.startswith("base_") and base[5:] in FIELD_LABELS and label_language() == "ar":
        return f"{FIELD_LABELS[base[5:]]} (عملة الشركة)"
    if doctype:
        try:
            import frappe

            field = frappe.get_meta(doctype).get_field(fieldname)
            if field and field.label:
                return translate(field.label)
        except Exception:
            pass
    return humanize(fieldname)


def doctype_label(doctype):
    if label_language() == "ar" and doctype in DOCTYPE_LABELS:
        return DOCTYPE_LABELS[doctype]
    return translate(doctype) if doctype else ""


def status_display(value):
    text = str(value or "").strip()
    label, tone = STATUS.get(text.lower(), (text, "neutral"))
    return label, tone


def looks_like_money(fieldname):
    key = str(fieldname or "").lower()
    if any(hint in key for hint in NOT_MONEY):
        return False
    return any(hint in key for hint in MONEY_HINTS)
