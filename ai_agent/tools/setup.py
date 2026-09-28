"""setup.check: read the actual ERPNext configuration for one area and report
what is missing against the setup-readiness checklist. Read only.

Company data goes through frappe.get_list (User Permissions apply); settings
singles are read only when the user has read permission on them.
"""

import frappe
from frappe.utils import cint, getdate

AREAS = ("company", "accounts", "stock", "selling", "buying")
OK, MISSING, WARN, UNKNOWN = "ok", "missing", "warn", "unknown"


def handle_check(arguments, permission_context):
	area = str(arguments.get("area") or "").strip().lower()
	if area not in AREAS:
		return {"error": f"area must be one of: {', '.join(AREAS)}"}
	company = arguments.get("company") or permission_context.company or frappe.defaults.get_user_default(
		"Company", permission_context.user
	)
	checks = CHECKS[area](company)
	return {
		"area": area,
		"company": company,
		"checks": checks,
		"missing": [c["item"] for c in checks if c["status"] == MISSING],
		"note": "Read only. Explain the steps; do not change settings (settings changes are not allowed through the agent).",
	}


def _item(item, status, detail, steps=""):
	row = {"item": item, "status": status, "detail": detail}
	if steps and status != OK:
		row["steps"] = steps
	return row


def _company(company, fields):
	if not company:
		return None
	try:
		rows = frappe.get_list("Company", filters={"name": company}, fields=fields, limit_page_length=1)
	except frappe.PermissionError:
		return None
	return rows[0] if rows else None


def _single(doctype, fields):
	if not frappe.has_permission(doctype, "read"):
		return None
	doc = frappe.get_cached_doc(doctype)
	return {field: doc.get(field) for field in fields}


def _count(doctype, filters):
	try:
		rows = frappe.get_list(doctype, filters=filters, fields=[{"COUNT": "name", "as": "n"}], limit_page_length=0)
	except frappe.PermissionError:
		return None
	return cint(rows[0].get("n")) if rows else 0


def _fields_check(row, required, where, steps):
	if row is None:
		return [_item(where, UNKNOWN, "لا تملك صلاحية قراءة هذه الإعدادات أو لم تُحدد شركة.")]
	return [
		_item(label, OK if row.get(field) else MISSING,
			  f"{label}: {row.get(field)}" if row.get(field) else f"{label} غير مضبوط في {where}.", steps)
		for field, label in required.items()
	]


def check_company(company):
	row = _company(company, ["name", "default_currency", "country", "abbr", "cost_center", "round_off_account",
							 "default_receivable_account", "default_payable_account"])
	checks = _fields_check(row, {"default_currency": "العملة الافتراضية", "country": "الدولة", "cost_center": "مركز التكلفة الافتراضي"},
						   f"الشركة {company}", "الشركة ← تعديل ← الحقل المذكور ← حفظ.")
	fiscal = _count("Fiscal Year", [["year_start_date", "<=", getdate()], ["year_end_date", ">=", getdate()], ["disabled", "=", 0]])
	checks.append(_item("السنة المالية الحالية", OK if fiscal else (UNKNOWN if fiscal is None else MISSING),
						"توجد سنة مالية تغطي تاريخ اليوم." if fiscal else "لا توجد سنة مالية مفعلة تغطي تاريخ اليوم.",
						"المحاسبة ← السنة المالية ← جديد: تاريخ البداية والنهاية ← حفظ."))
	warehouses = _count("Warehouse", {"company": company, "is_group": 0}) if company else None
	checks.append(_item("المخازن", OK if warehouses else (UNKNOWN if warehouses is None else MISSING),
						f"{warehouses} مخزن للشركة." if warehouses else "لا توجد مخازن فرعية للشركة.",
						"المخزون ← المخزن ← جديد ← اختر الشركة ← حفظ."))
	return checks


def check_accounts(company):
	row = _company(company, ["default_receivable_account", "default_payable_account", "default_income_account",
							 "default_expense_account", "round_off_account", "exchange_gain_loss_account",
							 "default_cash_account", "default_bank_account", "accounts_frozen_till_date"])
	checks = _fields_check(row, {
		"default_receivable_account": "حساب المدينين الافتراضي",
		"default_payable_account": "حساب الدائنين الافتراضي",
		"default_income_account": "حساب الإيرادات الافتراضي",
		"default_expense_account": "حساب المصروفات الافتراضي",
		"round_off_account": "حساب التقريب",
		"exchange_gain_loss_account": "حساب أرباح وخسائر فروق العملة",
		"default_cash_account": "حساب النقدية الافتراضي",
	}, f"الشركة {company}", "الشركة ← تبويب المحاسبة ← الحسابات الافتراضية ← اختر الحساب ← حفظ.")
	if row and row.get("accounts_frozen_till_date"):
		checks.append(_item("تجميد الحسابات", WARN, f"الحسابات مجمدة حتى {row.accounts_frozen_till_date}."))
	modes = _count("Mode of Payment Account", {"company": company}) if company else None
	checks.append(_item("حسابات طرق الدفع", OK if modes else (UNKNOWN if modes is None else MISSING),
						f"{modes} طريقة دفع مربوطة بحساب للشركة." if modes else "لا توجد طريقة دفع مربوطة بحساب لهذه الشركة.",
						"المحاسبة ← طريقة الدفع ← جدول الحسابات ← أضف الشركة والحساب الافتراضي."))
	return checks


def check_stock(company):
	settings = _single("Stock Settings", ["valuation_method", "default_warehouse", "stock_uom", "allow_negative_stock"])
	checks = _fields_check(settings, {"valuation_method": "طريقة التقييم", "stock_uom": "وحدة المخزون الافتراضية"},
						   "إعدادات المخزون", "المخزون ← إعدادات المخزون ← الحقل المذكور ← حفظ.")
	if settings and cint(settings.get("allow_negative_stock")):
		checks.append(_item("المخزون السالب", WARN, "المخزون السالب مسموح؛ قد تظهر أرصدة سالبة وتقييم غير دقيق."))
	row = _company(company, ["enable_perpetual_inventory", "default_inventory_account", "stock_adjustment_account",
							 "stock_received_but_not_billed"])
	if row and cint(row.get("enable_perpetual_inventory")):
		checks += _fields_check(row, {
			"default_inventory_account": "حساب المخزون الافتراضي",
			"stock_adjustment_account": "حساب تسوية المخزون",
			"stock_received_but_not_billed": "حساب مخزون مستلم غير مفوتر",
		}, f"الشركة {company}", "الشركة ← تبويب المخزون ← الحسابات ← حفظ.")
	items = _count("Item", {"is_stock_item": 1, "disabled": 0})
	checks.append(_item("أصناف مخزنية", OK if items else (UNKNOWN if items is None else MISSING),
						f"{items} صنف مخزني مفعّل." if items else "لا توجد أصناف مخزنية مفعلة."))
	return checks


def check_selling(company):
	settings = _single("Selling Settings", ["customer_group", "territory", "selling_price_list", "cust_master_name"])
	checks = _fields_check(settings, {
		"customer_group": "مجموعة العملاء الافتراضية",
		"territory": "المنطقة الافتراضية",
		"selling_price_list": "قائمة أسعار البيع الافتراضية",
	}, "إعدادات البيع", "المبيعات ← إعدادات البيع ← الحقل المذكور ← حفظ.")
	price_lists = _count("Price List", {"selling": 1, "enabled": 1})
	checks.append(_item("قوائم أسعار البيع", OK if price_lists else (UNKNOWN if price_lists is None else MISSING),
						f"{price_lists} قائمة أسعار بيع مفعلة." if price_lists else "لا توجد قائمة أسعار بيع مفعلة."))
	return checks


def check_buying(company):
	settings = _single("Buying Settings", ["supplier_group", "buying_price_list", "supp_master_name"])
	checks = _fields_check(settings, {
		"supplier_group": "مجموعة الموردين الافتراضية",
		"buying_price_list": "قائمة أسعار الشراء الافتراضية",
	}, "إعدادات الشراء", "المشتريات ← إعدادات الشراء ← الحقل المذكور ← حفظ.")
	suppliers = _count("Supplier", {"disabled": 0})
	checks.append(_item("الموردون", OK if suppliers else (UNKNOWN if suppliers is None else MISSING),
						f"{suppliers} مورد مفعّل." if suppliers else "لا يوجد موردون مفعّلون."))
	return checks


CHECKS = {
	"company": check_company,
	"accounts": check_accounts,
	"stock": check_stock,
	"selling": check_selling,
	"buying": check_buying,
}
