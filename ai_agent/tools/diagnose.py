"""diagnose.document: read-only checks explaining why a document does not
show where the user expects (stock ledger, general ledger, party statement,
item search).

Every read uses the run owner's permissions: the document itself through
has_permission, related records through frappe.get_list. A check the user
cannot evaluate (no permission on GL Entry, say) is reported as "unknown",
never guessed. Nothing is ever written; fixes come back as explanations.
"""

import frappe
from frappe.utils import cint, flt, getdate

PASS, FAIL, WARN, INFO, UNKNOWN = "pass", "fail", "warn", "info", "unknown"

STOCK_VOUCHERS = ("Sales Invoice", "Purchase Invoice", "Delivery Note", "Purchase Receipt", "Stock Entry", "Stock Reconciliation")
GL_VOUCHERS = ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry", "Delivery Note", "Purchase Receipt", "Stock Entry")
PARTY_VOUCHERS = {"Sales Invoice": ("Customer", "customer"), "Purchase Invoice": ("Supplier", "supplier")}
DOCSTATUS_LABEL = {0: "مسودة", 1: "معتمد", 2: "ملغى"}


def handle_document(arguments, permission_context):
	doctype = arguments.get("doctype")
	name = arguments.get("name")
	if not doctype or not name:
		return {"error": "doctype and name are required"}
	if not permission_context.has_doctype_permission(doctype, "read"):
		return {"error": f"No read permission on '{doctype}'"}
	try:
		doc = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		return {"error": "Document not found or not accessible."}
	if not permission_context.can_access_document(doc, "read"):
		return {"error": "Document not found or not accessible."}

	checks = [_docstatus_check(doc)]
	if doctype == "Item":
		checks += _item_search_checks(doc, arguments.get("company") or permission_context.company)
	else:
		if doctype in STOCK_VOUCHERS:
			checks += _stock_checks(doc)
		if doctype in GL_VOUCHERS:
			checks += _gl_checks(doc)
		if doctype in PARTY_VOUCHERS:
			checks += _party_statement_checks(doc)
	return {
		"doctype": doctype,
		"name": doc.name,
		"checks": checks,
		"failed": [c["check"] for c in checks if c["status"] == FAIL],
		"note": "Read-only diagnosis. Suggest fixes as steps; any document change needs a normal approval request.",
	}


def _check(check, status, detail):
	return {"check": check, "status": status, "detail": detail}


def _docstatus_check(doc):
	status = cint(doc.get("docstatus"))
	if status == 1:
		return _check("docstatus", PASS, "المستند معتمد (docstatus = 1).")
	return _check(
		"docstatus", FAIL,
		f"المستند {DOCSTATUS_LABEL.get(status, status)}: لا تُنشأ قيود محاسبية أو حركات مخزون إلا للمستندات المعتمدة.",
	)


def _count(doctype, filters):
	"""Rows the user can see, or None when they have no permission to look."""
	try:
		rows = frappe.get_list(doctype, filters=filters, fields=[{"COUNT": "name", "as": "n"}], limit_page_length=0)
	except frappe.PermissionError:
		return None
	return cint(rows[0].get("n")) if rows else 0


def _rows(doctype, filters, fields, limit=50, **kwargs):
	try:
		return frappe.get_list(doctype, filters=filters, fields=fields, limit_page_length=limit, **kwargs)
	except frappe.PermissionError:
		return None


# -- stock -------------------------------------------------------------------


def _stock_checks(doc):
	checks = []
	items = doc.get("items") or []
	if doc.doctype in ("Sales Invoice", "Purchase Invoice"):
		linked_field = "delivery_note" if doc.doctype == "Sales Invoice" else "purchase_receipt"
		linked = sorted({row.get(linked_field) for row in items if row.get(linked_field)})
		if cint(doc.get("update_stock")):
			checks.append(_check("update_stock", PASS, "خيار \"تحديث المخزون\" مفعّل في الفاتورة."))
		elif linked:
			checks.append(_check(
				"update_stock", INFO,
				f"الفاتورة لا تحدّث المخزون؛ حركة المخزون في المستند المرتبط: {', '.join(linked)}.",
			))
		else:
			checks.append(_check(
				"update_stock", FAIL,
				"خيار \"تحديث المخزون\" غير مفعّل ولا يوجد سند تسليم/استلام مرتبط، لذلك لا تنشأ حركة مخزون من هذه الفاتورة.",
			))

	item_codes = sorted({row.item_code for row in items if row.get("item_code")})
	flags = _rows("Item", [["name", "in", item_codes]], ["name", "is_stock_item"], limit=len(item_codes) or 1) if item_codes else []
	if flags is None:
		checks.append(_check("is_stock_item", UNKNOWN, "لا تملك صلاحية قراءة الأصناف للتحقق من كونها مخزنية."))
	else:
		non_stock = [row.name for row in flags if not cint(row.is_stock_item)]
		if non_stock and len(non_stock) == len(item_codes):
			checks.append(_check("is_stock_item", FAIL, f"كل الأصناف غير مخزنية ({', '.join(non_stock[:10])})؛ لا تتحرك في المخزون."))
		elif non_stock:
			checks.append(_check("is_stock_item", WARN, f"أصناف غير مخزنية لا تظهر في المخزون: {', '.join(non_stock[:10])}."))
		elif item_codes:
			checks.append(_check("is_stock_item", PASS, "كل الأصناف مخزنية."))

	stock_codes = {row.name for row in (flags or []) if cint(row.is_stock_item)}
	missing_wh = [row.item_code for row in items if row.get("item_code") in stock_codes and not row.get("warehouse") and not row.get("s_warehouse") and not row.get("t_warehouse")]
	if missing_wh:
		checks.append(_check("warehouse", FAIL, f"بنود مخزنية بلا مخزن: {', '.join(missing_wh[:10])}."))
	elif stock_codes:
		checks.append(_check("warehouse", PASS, "كل البنود المخزنية لها مخزن."))

	sle = _count("Stock Ledger Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0})
	if sle is None:
		checks.append(_check("stock_ledger_entry", UNKNOWN, "لا تملك صلاحية قراءة دفتر حركة المخزون (Stock Ledger Entry)."))
	elif sle:
		checks.append(_check("stock_ledger_entry", PASS, f"توجد {sle} حركة مخزون لهذا المستند."))
	else:
		checks.append(_check("stock_ledger_entry", FAIL if cint(doc.docstatus) == 1 else INFO, "لا توجد حركات مخزون مسجلة لهذا المستند."))
	return checks


# -- general ledger ----------------------------------------------------------


def _gl_checks(doc):
	checks = []
	gl = _count("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0})
	if gl is None:
		checks.append(_check("gl_entry", UNKNOWN, "لا تملك صلاحية قراءة القيود (GL Entry)."))
	elif gl:
		checks.append(_check("gl_entry", PASS, f"يوجد {gl} سطر قيد محاسبي لهذا المستند."))
	else:
		checks.append(_check("gl_entry", FAIL if cint(doc.docstatus) == 1 else INFO, "لا توجد قيود محاسبية لهذا المستند."))

	company = doc.get("company")
	if company:
		defaults = _rows("Company", [["name", "=", company]], [
			"default_receivable_account", "default_payable_account", "default_income_account",
			"default_expense_account", "round_off_account", "cost_center", "accounts_frozen_till_date",
		], limit=1)
		if defaults is None:
			checks.append(_check("default_accounts", UNKNOWN, "لا تملك صلاحية قراءة إعدادات الشركة."))
		elif defaults:
			row = defaults[0]
			needed = {"default_receivable_account": "حساب المدينين الافتراضي", "default_payable_account": "حساب الدائنين الافتراضي",
					  "default_income_account": "حساب الإيرادات الافتراضي", "round_off_account": "حساب التقريب",
					  "cost_center": "مركز التكلفة الافتراضي"}
			missing = [label for field, label in needed.items() if not row.get(field)]
			if missing:
				checks.append(_check("default_accounts", WARN, f"حسابات افتراضية ناقصة في الشركة {company}: {'، '.join(missing)}."))
			else:
				checks.append(_check("default_accounts", PASS, "الحسابات الافتراضية للشركة مضبوطة."))
			frozen = row.get("accounts_frozen_till_date")
			posting = doc.get("posting_date")
			if frozen and posting and getdate(posting) <= getdate(frozen):
				checks.append(_check("frozen_period", FAIL, f"الحسابات مجمدة حتى {frozen} وتاريخ المستند {posting}."))

	posting = doc.get("posting_date")
	if posting and company:
		periods = _rows("Accounting Period", [["company", "=", company], ["start_date", "<=", posting], ["end_date", ">=", posting]],
						["name"], limit=5)
		if periods:
			closed = _rows(
				"Closed Document",
				[["parent", "in", [p.name for p in periods]], ["document_type", "=", doc.doctype], ["closed", "=", 1]],
				["parent"], limit=5, parent_doctype="Accounting Period",
			)
			if closed:
				checks.append(_check("closed_period", FAIL, f"الفترة المحاسبية {closed[0].parent} مقفلة لهذا النوع من المستندات."))
		pcv = _rows("Period Closing Voucher", [["company", "=", company], ["docstatus", "=", 1], ["period_end_date", ">=", posting]],
					["name", "period_end_date"], limit=1)
		if pcv:
			checks.append(_check("period_closing", WARN, f"يوجد إقفال فترة ({pcv[0].name}) حتى {pcv[0].period_end_date} يغطي تاريخ المستند."))
	return checks


# -- party statement ---------------------------------------------------------


def _party_statement_checks(doc):
	party_type, party_field = PARTY_VOUCHERS[doc.doctype]
	party = doc.get(party_field)
	checks = []
	if not party:
		return [_check("party", FAIL, f"لا يوجد {party_type} في المستند.")]
	checks.append(_check("party", INFO, f"الطرف: {party_type} {party}، الشركة: {doc.get('company')}."))
	currency = doc.get("currency")
	party_currency = doc.get("party_account_currency")
	if party_currency and currency and party_currency != currency:
		checks.append(_check("currency", WARN, f"عملة الفاتورة {currency} تختلف عن عملة حساب الطرف {party_currency}؛ يظهر المبلغ في الكشف بعملة الحساب."))
	account = doc.get("debit_to") or doc.get("credit_to")
	if account:
		checks.append(_check("party_account", INFO, f"الحساب المستخدم: {account}. كشف الحساب يجب أن يُفلتر بنفس الشركة والحساب."))
	ple = _count("Payment Ledger Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "delinked": 0})
	if ple is None:
		checks.append(_check("payment_ledger", UNKNOWN, "لا تملك صلاحية قراءة دفتر الدفعات (Payment Ledger Entry)."))
	elif ple:
		checks.append(_check("payment_ledger", PASS, "للفاتورة سطر في دفتر الدفعات، فهي تظهر في كشوف الطرف والأعمار."))
	else:
		checks.append(_check("payment_ledger", FAIL if cint(doc.docstatus) == 1 else INFO, "لا يوجد للفاتورة سطر في دفتر الدفعات."))
	checks.append(_check("outstanding", INFO, f"المتبقي: {flt(doc.get('outstanding_amount'))}."))
	return checks


# -- item search -------------------------------------------------------------


def _item_search_checks(item, company=None):
	checks = []
	if cint(item.disabled):
		checks.append(_check("disabled", FAIL, "الصنف معطّل (disabled)."))
	else:
		checks.append(_check("disabled", PASS, "الصنف غير معطّل."))
	if item.get("end_of_life") and getdate(item.end_of_life) < getdate():
		checks.append(_check("end_of_life", FAIL, f"انتهى عمر الصنف في {item.end_of_life}."))
	if cint(item.has_variants):
		checks.append(_check("has_variants", WARN, "هذا صنف قالب (Template): تظهر المتغيرات في الحركات وليس القالب نفسه."))
	checks.append(_check(
		"is_sales_item", PASS if cint(item.is_sales_item) else FAIL,
		"يظهر في مستندات البيع." if cint(item.is_sales_item) else "غير مفعّل كصنف بيع (is_sales_item)؛ لا يظهر في مستندات البيع.",
	))
	checks.append(_check(
		"is_purchase_item", PASS if cint(item.is_purchase_item) else FAIL,
		"يظهر في مستندات الشراء." if cint(item.is_purchase_item) else "غير مفعّل كصنف شراء (is_purchase_item)؛ لا يظهر في مستندات الشراء.",
	))
	if company:
		defaults = [row for row in (item.get("item_defaults") or []) if row.company == company]
		if not defaults:
			checks.append(_check("item_default", WARN, f"لا توجد إعدادات افتراضية للصنف (Item Default) للشركة {company}."))
		elif cint(item.is_stock_item) and not defaults[0].default_warehouse:
			checks.append(_check("item_default", WARN, f"لا يوجد مخزن افتراضي للصنف في الشركة {company}."))
		else:
			checks.append(_check("item_default", PASS, f"الإعدادات الافتراضية للشركة {company} موجودة."))
	return checks
