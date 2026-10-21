"""diagnose.document: read-only checks explaining why a document does not
show where the user expects (stock ledger, general ledger, party statement,
item search).

Every read uses the run owner's permissions: the document itself through
has_permission, related records through frappe.get_list. A check the user
cannot evaluate (no permission on GL Entry, say) is reported as "unknown",
never guessed. Nothing is ever written; fixes come back as explanations.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate

PASS, FAIL, WARN, INFO, UNKNOWN = "pass", "fail", "warn", "info", "unknown"

STOCK_VOUCHERS = ("Sales Invoice", "Purchase Invoice", "Delivery Note", "Purchase Receipt", "Stock Entry", "Stock Reconciliation")
GL_VOUCHERS = ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry", "Delivery Note", "Purchase Receipt", "Stock Entry")
PARTY_VOUCHERS = {"Sales Invoice": ("Customer", "customer"), "Purchase Invoice": ("Supplier", "supplier")}
DOCSTATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}   # translated where used


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
		return _check("docstatus", PASS, _("The document is submitted (docstatus = 1)."))
	return _check(
		"docstatus", FAIL,
		_("The document is {0}: accounting entries and stock movements are created only for submitted documents.").format(_(DOCSTATUS_LABEL[status], context="AI Agent tools") if status in DOCSTATUS_LABEL else status),
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
			checks.append(_check("update_stock", PASS, _("The \"Update Stock\" option is enabled on the invoice.")))
		elif linked:
			checks.append(_check(
				"update_stock", INFO,
				_("The invoice does not update stock; the stock movement is in the linked document: {0}.").format(', '.join(linked)),
			))
		else:
			checks.append(_check(
				"update_stock", FAIL,
				_("The \"Update Stock\" option is off and no delivery note or purchase receipt is linked, so this invoice creates no stock movement."),
			))

	item_codes = sorted({row.item_code for row in items if row.get("item_code")})
	flags = _rows("Item", [["name", "in", item_codes]], ["name", "is_stock_item"], limit=len(item_codes) or 1) if item_codes else []
	if flags is None:
		checks.append(_check("is_stock_item", UNKNOWN, _("You do not have permission to read the items to check whether they are stock items.")))
	else:
		non_stock = [row.name for row in flags if not cint(row.is_stock_item)]
		if non_stock and len(non_stock) == len(item_codes):
			checks.append(_check("is_stock_item", FAIL, _("None of the items is a stock item ({0}); they do not move in stock.").format(', '.join(non_stock[:10]))))
		elif non_stock:
			checks.append(_check("is_stock_item", WARN, _("Non-stock items that do not appear in stock: {0}.").format(', '.join(non_stock[:10]))))
		elif item_codes:
			checks.append(_check("is_stock_item", PASS, _("All items are stock items.")))

	stock_codes = {row.name for row in (flags or []) if cint(row.is_stock_item)}
	missing_wh = [row.item_code for row in items if row.get("item_code") in stock_codes and not row.get("warehouse") and not row.get("s_warehouse") and not row.get("t_warehouse")]
	if missing_wh:
		checks.append(_check("warehouse", FAIL, _("Stock rows without a warehouse: {0}.").format(', '.join(missing_wh[:10]))))
	elif stock_codes:
		checks.append(_check("warehouse", PASS, _("Every stock row has a warehouse.")))

	sle = _count("Stock Ledger Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0})
	if sle is None:
		checks.append(_check("stock_ledger_entry", UNKNOWN, _("You do not have permission to read the stock ledger (Stock Ledger Entry).")))
	elif sle:
		checks.append(_check("stock_ledger_entry", PASS, _("{0} stock movements exist for this document.").format(sle)))
	else:
		checks.append(_check("stock_ledger_entry", FAIL if cint(doc.docstatus) == 1 else INFO, _("No stock movements are recorded for this document.")))
	return checks


# -- general ledger ----------------------------------------------------------


def _gl_checks(doc):
	checks = []
	gl = _count("GL Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "is_cancelled": 0})
	if gl is None:
		checks.append(_check("gl_entry", UNKNOWN, _("You do not have permission to read the ledger entries (GL Entry).")))
	elif gl:
		checks.append(_check("gl_entry", PASS, _("{0} general ledger lines exist for this document.").format(gl)))
	else:
		checks.append(_check("gl_entry", FAIL if cint(doc.docstatus) == 1 else INFO, _("No accounting entries exist for this document.")))

	company = doc.get("company")
	if company:
		defaults = _rows("Company", [["name", "=", company]], [
			"default_receivable_account", "default_payable_account", "default_income_account",
			"default_expense_account", "round_off_account", "cost_center", "accounts_frozen_till_date",
		], limit=1)
		if defaults is None:
			checks.append(_check("default_accounts", UNKNOWN, _("You do not have permission to read the company settings.")))
		elif defaults:
			row = defaults[0]
			needed = {"default_receivable_account": _("Default Receivable Account", context="AI Agent tools"), "default_payable_account": _("Default Payable Account", context="AI Agent tools"),
					  "default_income_account": _("Default Income Account", context="AI Agent tools"), "round_off_account": _("Round Off Account", context="AI Agent tools"),
					  "cost_center": _("Default Cost Center")}
			missing = [label for field, label in needed.items() if not row.get(field)]
			if missing:
				checks.append(_check("default_accounts", WARN, _("Default accounts missing in company {0}: {1}.").format(company, ", ".join(missing))))
			else:
				checks.append(_check("default_accounts", PASS, _("The company's default accounts are set.")))
			frozen = row.get("accounts_frozen_till_date")
			posting = doc.get("posting_date")
			if frozen and posting and getdate(posting) <= getdate(frozen):
				checks.append(_check("frozen_period", FAIL, _("Accounts are frozen until {0} and the document date is {1}.").format(frozen, posting)))

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
				checks.append(_check("closed_period", FAIL, _("Accounting period {0} is closed for this document type.").format(closed[0].parent)))
		pcv = _rows("Period Closing Voucher", [["company", "=", company], ["docstatus", "=", 1], ["period_end_date", ">=", posting]],
					["name", "period_end_date"], limit=1)
		if pcv:
			checks.append(_check("period_closing", WARN, _("A period closing ({0}) up to {1} covers the document date.").format(pcv[0].name, pcv[0].period_end_date)))
	return checks


# -- party statement ---------------------------------------------------------


def _party_statement_checks(doc):
	party_type, party_field = PARTY_VOUCHERS[doc.doctype]
	party = doc.get(party_field)
	checks = []
	if not party:
		return [_check("party", FAIL, _("No {0} on the document.").format(_(party_type)))]
	checks.append(_check("party", INFO, _("Party: {0} {1}, company: {2}.").format(_(party_type), party, doc.get("company"))))
	currency = doc.get("currency")
	party_currency = doc.get("party_account_currency")
	if party_currency and currency and party_currency != currency:
		checks.append(_check("currency", WARN, _("The invoice currency {0} differs from the party account currency {1}; the statement shows the amount in the account currency.").format(currency, party_currency)))
	account = doc.get("debit_to") or doc.get("credit_to")
	if account:
		checks.append(_check("party_account", INFO, _("Account used: {0}. The statement must be filtered by the same company and account.").format(account)))
	ple = _count("Payment Ledger Entry", {"voucher_type": doc.doctype, "voucher_no": doc.name, "delinked": 0})
	if ple is None:
		checks.append(_check("payment_ledger", UNKNOWN, _("You do not have permission to read the payment ledger (Payment Ledger Entry).")))
	elif ple:
		checks.append(_check("payment_ledger", PASS, _("The invoice has a payment ledger row, so it appears in the party statements and ageing.")))
	else:
		checks.append(_check("payment_ledger", FAIL if cint(doc.docstatus) == 1 else INFO, _("The invoice has no payment ledger row.")))
	checks.append(_check("outstanding", INFO, _("Outstanding: {0}.").format(flt(doc.get("outstanding_amount")))))
	return checks


# -- item search -------------------------------------------------------------


def _item_search_checks(item, company=None):
	checks = []
	if cint(item.disabled):
		checks.append(_check("disabled", FAIL, _("The item is disabled.")))
	else:
		checks.append(_check("disabled", PASS, _("The item is not disabled.")))
	if item.get("end_of_life") and getdate(item.end_of_life) < getdate():
		checks.append(_check("end_of_life", FAIL, _("The item reached its end of life on {0}.").format(item.end_of_life)))
	if cint(item.has_variants):
		checks.append(_check("has_variants", WARN, _("This is a template item: its variants appear in transactions, not the template itself.")))
	checks.append(_check(
		"is_sales_item", PASS if cint(item.is_sales_item) else FAIL,
		_("Appears in sales documents.") if cint(item.is_sales_item) else _("Not enabled as a sales item (is_sales_item); it does not appear in sales documents."),
	))
	checks.append(_check(
		"is_purchase_item", PASS if cint(item.is_purchase_item) else FAIL,
		_("Appears in purchase documents.") if cint(item.is_purchase_item) else _("Not enabled as a purchase item (is_purchase_item); it does not appear in purchase documents."),
	))
	if company:
		defaults = [row for row in (item.get("item_defaults") or []) if row.company == company]
		if not defaults:
			checks.append(_check("item_default", WARN, _("No item defaults (Item Default) for company {0}.").format(company)))
		elif cint(item.is_stock_item) and not defaults[0].default_warehouse:
			checks.append(_check("item_default", WARN, _("No default warehouse for the item in company {0}.").format(company)))
		else:
			checks.append(_check("item_default", PASS, _("The item defaults for company {0} exist.").format(company)))
	return checks
