"""Standard ERPNext functions exposed as agent tools, in the user's session.

Read tools wrap ERPNext's own helpers (item details, party details, fiscal
year, exchange rate, stock balance, invoice payments) after a row-level
permission check on the records they touch, and return trimmed results.

actions.request_mapped_draft builds a draft with ERPNext's standard make_*
mapping functions (so links like so_detail / against_sales_order are set the
way ERPNext expects) and only creates an approval request: the draft is
inserted by the Approval Engine after a human approves it.
"""

import frappe
from frappe.utils import flt, getdate, nowdate

from ai_agent.runtime.approval_engine import ApprovalEngine
from ai_agent.runtime.exceptions import PermissionDenied
from ai_agent.runtime.write_guard import blocked_fields

MAPPERS = {
	("Sales Order", "Sales Invoice"): "erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
	("Sales Order", "Delivery Note"): "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note",
	("Material Request", "Purchase Order"): "erpnext.stock.doctype.material_request.material_request.make_purchase_order",
	("Purchase Invoice", "Purchase Receipt"): "erpnext.accounts.doctype.purchase_invoice.purchase_invoice.make_purchase_receipt",
}
NOT_ACCESSIBLE = "Document not found or not accessible."


def _readable_doc(doctype, name, ctx):
	if not doctype or not name or not ctx.has_doctype_permission(doctype, "read"):
		return None
	try:
		doc = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		return None
	return doc if ctx.can_access_document(doc, "read") else None


def _pick(data, keys):
	return {key: data.get(key) for key in keys if data.get(key) not in (None, "")}


# -- read tools --------------------------------------------------------------


def handle_item_details(arguments, ctx):
	from erpnext.stock.get_item_details import get_item_details

	item = _readable_doc("Item", arguments.get("item_code"), ctx)
	if not item:
		return {"error": NOT_ACCESSIBLE}
	company = arguments.get("company") or ctx.company
	if company and not _readable_doc("Company", company, ctx):
		return {"error": NOT_ACCESSIBLE}
	doctype = arguments.get("doctype") or "Sales Invoice"
	selling = doctype in ("Quotation", "Sales Order", "Delivery Note", "Sales Invoice", "POS Invoice")
	details_ctx = frappe._dict({
		"item_code": item.name,
		"company": company,
		"doctype": doctype,
		"transaction_date": arguments.get("date") or nowdate(),
		"posting_date": arguments.get("date") or nowdate(),
		"qty": flt(arguments.get("qty") or 1),
		"customer": arguments.get("customer") if selling else None,
		"supplier": arguments.get("supplier") if not selling else None,
		"price_list": arguments.get("price_list"),
		"currency": arguments.get("currency"),
		"conversion_rate": 1,
		"plc_conversion_rate": 1,
		"warehouse": arguments.get("warehouse"),
	})
	try:
		details = get_item_details(details_ctx)
	except Exception as exc:
		return {"error": f"ERPNext could not compute item details: {exc}"}
	return _pick(details, (
		"item_code", "item_name", "uom", "stock_uom", "conversion_factor", "price_list_rate", "rate", "discount_percentage",
		"warehouse", "actual_qty", "projected_qty", "income_account", "expense_account", "cost_center", "item_tax_template",
		"valuation_rate", "is_stock_item",
	))


def handle_party_details(arguments, ctx):
	from erpnext.accounts.party import get_party_details

	party_type = arguments.get("party_type") or "Customer"
	if party_type not in ("Customer", "Supplier"):
		return {"error": "party_type must be Customer or Supplier"}
	party = _readable_doc(party_type, arguments.get("party"), ctx)
	company = arguments.get("company") or ctx.company
	if not party or (company and not _readable_doc("Company", company, ctx)):
		return {"error": NOT_ACCESSIBLE}
	try:
		details = get_party_details(
			party=party.name, party_type=party_type, company=company,
			posting_date=arguments.get("date") or nowdate(),
			doctype=arguments.get("doctype") or ("Sales Invoice" if party_type == "Customer" else "Purchase Invoice"),
			ignore_permissions=False,
		)
	except Exception as exc:
		return {"error": f"ERPNext could not compute party details: {exc}"}
	return _pick(details, (
		"customer", "customer_name", "supplier", "supplier_name", "debit_to", "credit_to", "party_account_currency",
		"currency", "price_list", "selling_price_list", "buying_price_list", "payment_terms_template", "taxes_and_charges",
		"tax_category", "customer_group", "territory", "sales_team", "contact_person", "customer_address", "supplier_address",
	))


def handle_fiscal_year(arguments, ctx):
	from erpnext.accounts.utils import get_fiscal_year

	company = arguments.get("company") or ctx.company
	try:
		name, start, end = get_fiscal_year(getdate(arguments.get("date") or nowdate()), company=company, verbose=0)
	except Exception as exc:
		return {"error": str(exc)}
	return {"fiscal_year": name, "from": str(start), "to": str(end), "company": company}


def handle_exchange_rate(arguments, ctx):
	from erpnext.setup.utils import get_exchange_rate

	source, target = arguments.get("from_currency"), arguments.get("to_currency")
	if not source or not target:
		return {"error": "from_currency and to_currency are required"}
	date = arguments.get("date") or nowdate()
	rate = get_exchange_rate(source, target, date)
	return {"from_currency": source, "to_currency": target, "date": date, "rate": rate}


def handle_stock_balance(arguments, ctx):
	from erpnext.stock.utils import get_stock_balance

	item = _readable_doc("Item", arguments.get("item_code"), ctx)
	warehouse = _readable_doc("Warehouse", arguments.get("warehouse"), ctx)
	if not item or not warehouse:
		return {"error": NOT_ACCESSIBLE}
	if not ctx.has_doctype_permission("Stock Ledger Entry", "read"):
		return {"error": "No read permission on stock ledger."}
	date = arguments.get("date") or nowdate()
	qty, rate = get_stock_balance(item.name, warehouse.name, date, with_valuation_rate=True)[:2]
	return {"item_code": item.name, "warehouse": warehouse.name, "date": date, "qty": qty, "valuation_rate": rate,
			"stock_value": flt(qty) * flt(rate)}


def handle_invoice_payments(arguments, ctx):
	doctype = arguments.get("doctype")
	if doctype not in ("Sales Invoice", "Purchase Invoice"):
		return {"error": "doctype must be Sales Invoice or Purchase Invoice"}
	invoice = _readable_doc(doctype, arguments.get("name"), ctx)
	if not invoice:
		return {"error": NOT_ACCESSIBLE}
	try:
		rows = frappe.get_list(
			"Payment Ledger Entry",
			filters={"against_voucher_type": doctype, "against_voucher_no": invoice.name, "delinked": 0,
					 "voucher_no": ["!=", invoice.name]},
			fields=["voucher_type", "voucher_no", "posting_date", "amount", "account_currency"],
			order_by="posting_date asc",
			limit_page_length=50,
		)
	except frappe.PermissionError:
		return {"error": "No read permission on payment ledger."}
	return {
		"invoice": invoice.name,
		"grand_total": invoice.get("grand_total"),
		"outstanding_amount": invoice.get("outstanding_amount"),
		"payments": [{**row, "amount": abs(flt(row.amount))} for row in rows],
	}


# -- mapped drafts (approval only) --------------------------------------------


def build_mapped_payload(source_doctype, source_name, target_doctype, ctx):
	"""Draft payload from ERPNext's standard mapper; raises PermissionDenied."""
	path = MAPPERS.get((source_doctype, target_doctype))
	if not path:
		supported = ", ".join(f"{s} → {t}" for s, t in MAPPERS)
		raise PermissionDenied(f"Unsupported mapping. Supported: {supported}.")
	if not _readable_doc(source_doctype, source_name, ctx):
		raise PermissionDenied(NOT_ACCESSIBLE)
	if not frappe.has_permission(target_doctype, "create", user=ctx.user):
		raise PermissionDenied(f"No create permission on {target_doctype}.")
	target = frappe.get_attr(path)(source_name)
	payload = _clean(target.as_dict(no_default_fields=True))
	payload["doctype"] = target_doctype
	payload["docstatus"] = 0
	# Fields the mapper filled at permlevels this user cannot write are dropped.
	for problem in blocked_fields(target_doctype, payload, ctx.user, mode="create"):
		_drop(payload, problem)
	return payload


def _clean(value):
	if isinstance(value, dict):
		return {
			key: _clean(item) for key, item in value.items()
			if not str(key).startswith("__") and item is not None
			and key not in ("name", "owner", "creation", "modified", "modified_by", "parent", "parenttype", "parentfield", "idx")
		}
	if isinstance(value, list):
		return [_clean(item) for item in value]
	return value


def _drop(payload, path):
	if "[" in path:
		table, rest = path.split("[", 1)
		index, field = rest.split("].", 1)
		rows = payload.get(table) or []
		if int(index) < len(rows):
			rows[int(index)].pop(field, None)
	else:
		payload.pop(path, None)


def handle_request_mapped_draft(arguments, ctx):
	source_doctype = arguments.get("source_doctype")
	source_name = arguments.get("source_name")
	target_doctype = arguments.get("target_doctype")
	try:
		payload = build_mapped_payload(source_doctype, source_name, target_doctype, ctx)
		approval = ApprovalEngine(ctx).create_approval_request(
			run_name=arguments.get("run") or getattr(ctx, "run_name", None),
			action_type="Create",
			doctype_name=target_doctype,
			action_summary=f"Create {target_doctype} draft from {source_doctype} {source_name}",
			impact_summary=f"A new {target_doctype} draft will be created from {source_name} using ERPNext's standard mapping.",
			payload=payload,
		)
	except PermissionDenied as exc:
		return {"error": str(exc)}
	return {"approval_required": True, "approval_id": approval.name, "action_summary": approval.action_summary}
