"""setup.check: read the actual ERPNext configuration for one area and report
what is missing against the setup checklist of the system skill. Read only.

Company data goes through frappe.get_list (User Permissions apply); settings
singles are read only when the user has read permission on them.
"""

import frappe
from frappe import _
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
		return [_item(where, UNKNOWN, _("You do not have permission to read these settings, or no company is set."))]
	return [
		_item(label, OK if row.get(field) else MISSING,
			  f"{label}: {row.get(field)}" if row.get(field) else _("{0} is not set in {1}.").format(label, where), steps)
		for field, label in required.items()
	]


def check_company(company):
	row = _company(company, ["name", "default_currency", "country", "abbr", "cost_center", "round_off_account",
							 "default_receivable_account", "default_payable_account"])
	checks = _fields_check(row, {"default_currency": _("Default Currency"), "country": _("Country"), "cost_center": _("Default Cost Center")},
						   _("Company {0}").format(company), _("Company → Edit → the field named → Save."))
	fiscal = _count("Fiscal Year", [["year_start_date", "<=", getdate()], ["year_end_date", ">=", getdate()], ["disabled", "=", 0]])
	checks.append(_item(_("Current Fiscal Year"), OK if fiscal else (UNKNOWN if fiscal is None else MISSING),
						_("A fiscal year covers today's date.") if fiscal else _("No enabled fiscal year covers today's date."),
						_("Accounting → Fiscal Year → New: start and end dates → Save.")))
	warehouses = _count("Warehouse", {"company": company, "is_group": 0}) if company else None
	checks.append(_item(_("Warehouses", context="AI Agent tools"), OK if warehouses else (UNKNOWN if warehouses is None else MISSING),
						_("{0} warehouses for the company.").format(warehouses) if warehouses else _("The company has no non-group warehouses."),
						_("Stock → Warehouse → New → choose the company → Save.")))
	return checks


def check_accounts(company):
	row = _company(company, ["default_receivable_account", "default_payable_account", "default_income_account",
							 "default_expense_account", "round_off_account", "exchange_gain_loss_account",
							 "default_cash_account", "default_bank_account", "accounts_frozen_till_date"])
	checks = _fields_check(row, {
		"default_receivable_account": _("Default Receivable Account", context="AI Agent tools"),
		"default_payable_account": _("Default Payable Account", context="AI Agent tools"),
		"default_income_account": _("Default Income Account", context="AI Agent tools"),
		"default_expense_account": _("Default Expense Account"),
		"round_off_account": _("Round Off Account", context="AI Agent tools"),
		"exchange_gain_loss_account": _("Exchange Gain/Loss Account"),
		"default_cash_account": _("Default Cash Account", context="AI Agent tools"),
	}, _("Company {0}").format(company), _("Company → Accounts tab → Default Accounts → choose the account → Save."))
	if row and row.get("accounts_frozen_till_date"):
		checks.append(_item(_("Accounts Freeze"), WARN, _("Accounts are frozen until {0}.").format(row.accounts_frozen_till_date)))
	modes = _count("Mode of Payment Account", {"company": company}) if company else None
	checks.append(_item(_("Mode of Payment Accounts"), OK if modes else (UNKNOWN if modes is None else MISSING),
						_("{0} modes of payment linked to an account for the company.").format(modes) if modes else _("No mode of payment is linked to an account for this company."),
						_("Accounting → Mode of Payment → Accounts table → add the company and its default account.")))
	return checks


def check_stock(company):
	settings = _single("Stock Settings", ["valuation_method", "default_warehouse", "stock_uom", "allow_negative_stock"])
	checks = _fields_check(settings, {"valuation_method": _("Valuation Method"), "stock_uom": _("Default Stock UOM", context="AI Agent tools")},
						   _("Stock Settings"), _("Stock → Stock Settings → the field named → Save."))
	if settings and cint(settings.get("allow_negative_stock")):
		checks.append(_item(_("Negative Stock"), WARN, _("Negative stock is allowed; negative balances and inaccurate valuation may appear.")))
	row = _company(company, ["enable_perpetual_inventory", "default_inventory_account", "stock_adjustment_account",
							 "stock_received_but_not_billed"])
	if row and cint(row.get("enable_perpetual_inventory")):
		checks += _fields_check(row, {
			"default_inventory_account": _("Default Inventory Account"),
			"stock_adjustment_account": _("Stock Adjustment Account", context="AI Agent tools"),
			"stock_received_but_not_billed": _("Stock Received But Not Billed", context="AI Agent tools"),
		}, _("Company {0}").format(company), _("Company → Stock tab → Accounts → Save."))
	items = _count("Item", {"is_stock_item": 1, "disabled": 0})
	checks.append(_item(_("Stock Items", context="AI Agent tools"), OK if items else (UNKNOWN if items is None else MISSING),
						_("{0} enabled stock items.").format(items) if items else _("No enabled stock items.")))
	return checks


def check_selling(company):
	settings = _single("Selling Settings", ["customer_group", "territory", "selling_price_list", "cust_master_name"])
	checks = _fields_check(settings, {
		"customer_group": _("Default Customer Group", context="AI Agent tools"),
		"territory": _("Default Territory", context="AI Agent tools"),
		"selling_price_list": _("Default Selling Price List"),
	}, _("Selling Settings"), _("Selling → Selling Settings → the field named → Save."))
	price_lists = _count("Price List", {"selling": 1, "enabled": 1})
	checks.append(_item(_("Selling Price Lists"), OK if price_lists else (UNKNOWN if price_lists is None else MISSING),
						_("{0} enabled selling price lists.").format(price_lists) if price_lists else _("No enabled selling price list.")))
	return checks


def check_buying(company):
	settings = _single("Buying Settings", ["supplier_group", "buying_price_list", "supp_master_name"])
	checks = _fields_check(settings, {
		"supplier_group": _("Default Supplier Group"),
		"buying_price_list": _("Default Buying Price List", context="AI Agent tools"),
	}, _("Buying Settings"), _("Buying → Buying Settings → the field named → Save."))
	suppliers = _count("Supplier", {"disabled": 0})
	checks.append(_item(_("Suppliers", context="AI Agent tools"), OK if suppliers else (UNKNOWN if suppliers is None else MISSING),
						_("{0} enabled suppliers.").format(suppliers) if suppliers else _("No enabled suppliers.")))
	return checks


CHECKS = {
	"company": check_company,
	"accounts": check_accounts,
	"stock": check_stock,
	"selling": check_selling,
	"buying": check_buying,
}
