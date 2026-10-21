"""Business vocabulary: field labels, status badges and field priority tiers (English, shown translated).

Everything here is server-defined so the same field always reads and ranks the
same way, whatever the LLM or the DocType field order happens to be.
"""

import re

# Tier 1 primary, 2 operational, 3 secondary, 4 technical (hidden by default).
TIER_TECHNICAL = 4

FIELD_LABELS = {
    "name": "Document",
    "customer": "Customer",
    "customer_name": "Customer",
    "supplier": "Supplier",
    "supplier_name": "Supplier",
    "party": "Party",
    "party_name": "Party",
    "company": "Company",
    "company_name": "Company",
    "default_currency": "Currency",
    "currency": "Currency",
    "account_currency": "Currency",
    "grand_total": "Grand Total",
    "base_grand_total": "Grand Total (company currency)",
    "rounded_total": "Rounded Total",
    "total": "Items Total",
    "net_total": "Net Total (before tax)",
    "outstanding_amount": "Outstanding",
    "total_advance": "Advance Payments",
    "discount_amount": "Discount",
    "conversion_rate": "Exchange Rate",
    "stock_qty": "Qty in Stock UOM",
    "uom": "Unit",
    "stock_uom": "Stock Unit",
    "conversion_factor": "Conversion Factor",
    "total_qty": "Total Qty",
    "update_stock": "Update Stock",
    "set_warehouse": "Warehouse",
    "debit_to": "Receivable Account",
    "credit_to": "Payable Account",
    "total_debit": "Total Debit",
    "total_credit": "Total Credit",
    "received_amount": "Amount Received",
    "paid_from": "Paid From",
    "paid_to": "Paid To",
    "payment_type": "Payment Type",
    "reference_no": "Reference No.",
    "reference_date": "Reference Date",
    "tax_amount": "Tax Amount",
    "account_head": "Tax Account",
    "description": "Description",
    "allocated_amount": "Allocated Amount",
    "reference_name": "Reference Document",
    "reference_doctype": "Reference Type",
    "base_amount": "Amount (company currency)",
    "net_amount": "Net Amount",
    "is_return": "Return",
    "is_pos": "POS",
    "is_opening": "Opening Entry",
    "tax_id": "Tax ID",
    "payment_terms_template": "Payment Terms",
    "taxes_and_charges": "Tax Template",
    "tax_category": "Tax Category",
    "total_taxes_and_charges": "Taxes and Charges",
    "selling_price_list": "Price List",
    "buying_price_list": "Price List",
    "against_income_account": "Income Account",
    "bill_no": "Supplier Invoice No.",
    "bill_date": "Supplier Invoice Date",
    "workflow_state": "Workflow State",
    "item_group": "Item Group",
    "actual_qty": "In Stock",
    "paid_amount": "Paid Amount",
    "amount": "Amount",
    "rate": "Rate",
    "qty": "Qty",
    "projected_qty": "Projected Qty",
    "reserved_qty": "Reserved",
    "stock_value": "Stock Value",
    "valuation_rate": "Valuation Rate",
    "item_code": "Item",
    "item_name": "Item Name",
    "warehouse": "Warehouse",
    "posting_date": "Date",
    "transaction_date": "Date",
    "due_date": "Due Date",
    "status": "Status",
    "debit": "Debit",
    "credit": "Credit",
    "balance": "Balance",
    "account": "Account",
    "voucher_type": "Type",
    "voucher_no": "Document",
    "remarks": "Remarks",
    "against": "Against",
    "cost_center": "Cost Center",
    "branch": "Branch",
    "project": "Project",
    "territory": "Territory",
    "customer_group": "Customer Group",
    "supplier_group": "Supplier Group",
    "sales_person": "Sales Person",
    "mode_of_payment": "Mode of Payment",
    "total_number_of_shares": "Shares",
    "country": "Country",
    "abbr": "Abbreviation",
    "domain": "Business Domain",
    "date_of_establishment": "Date of Establishment",
    "capital": "Capital",
    "paid_up_capital": "Paid-up Capital",
    "value": "Value",
    "total_sales": "Sales",
    "previous": "Previous",
    "change": "Change",
    "percentage": "Percentage",
    "share": "Share",
    "count": "Count",
    "invoices": "Invoices",
    "in_words": "Amount in Words",
    "posting_time": "Time",
    "creation": "Created On",
    "modified": "Last Modified",
    "owner": "Created By",
}

# DocTypes with a business name of their own in the views (translated with the AI View context).
DOCTYPE_LABELS = {
    "Sales Invoice",
    "Purchase Invoice",
    "Payment Entry",
    "Journal Entry",
    "Sales Order",
    "Purchase Order",
    "Delivery Note",
    "Purchase Receipt",
    "Stock Entry",
    "Quotation",
    "Customer",
    "Supplier",
    "Item",
    "Company",
    "Warehouse",
    "GL Entry",
    "Stock Ledger Entry",
    "Material Request",
}

DOCTYPE_PLURALS = {
    "Sales Invoice": "Sales Invoices",
    "Purchase Invoice": "Purchase Invoices",
    "Payment Entry": "Payment Entries",
    "Journal Entry": "Journal Entries",
    "Customer": "Customers",
    "Supplier": "Suppliers",
    "Item": "Items",
    "Company": "Companies",
    "Warehouse": "Warehouses",
    "GL Entry": "GL Entries",
    "Stock Ledger Entry": "Stock Movements",
}

# (label, tone). Tones are semantic only: success warning danger info neutral.
STATUS = {
    "draft": ("Draft", "neutral"),
    "submitted": ("Submitted", "info"),
    "paid": ("Paid", "success"),
    "partly paid": ("Partly Paid", "warning"),
    "unpaid": ("Unpaid", "warning"),
    "overdue": ("Overdue", "danger"),
    "cancelled": ("Cancelled", "neutral"),
    "return": ("Return", "neutral"),
    "credit note issued": ("Credit Note Issued", "neutral"),
    "completed": ("Completed", "success"),
    "to deliver and bill": ("To Deliver and Bill", "info"),
    "to bill": ("To Bill", "info"),
    "to deliver": ("To Deliver", "info"),
    "closed": ("Closed", "neutral"),
    "open": ("Open", "info"),
    "pending": ("Awaiting Approval", "warning"),
    "approved": ("Approved", "success"),
    "rejected": ("Rejected", "danger"),
    "executed": ("Executed", "success"),
    "expired": ("Expired", "neutral"),
    "failed": ("Failed", "danger"),
    "enabled": ("Enabled", "success"),
    "disabled": ("Disabled", "neutral"),
    "active": ("Active", "success"),
    "inactive": ("Inactive", "neutral"),
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
        # Request-scoped: frappe.local attributes live for one request on one site. (The instance
        # __dict__ of frappe.local outlives requests and is shared by every site in the worker.)
        cache = getattr(frappe.local, "ai_label_lang", None)
        if cache is None:
            cache = frappe.local.ai_label_lang = {}
        if user not in cache:
            cache[user] = frappe.db.get_value("User", user, "language") or frappe.local.lang or "ar"
        return cache[user]
    except Exception:
        # Cosmetic: labels fall back to the default language.
        return "ar"


def translate(text):
    try:
        import frappe

        return frappe._(text, lang=label_language())
    except Exception:
        # Cosmetic: the untranslated text is shown.
        return text


def label_for(fieldname, doctype=None):
    """The business label of a field in the viewer's language."""
    from ai_agent.presentation.i18n import t

    if fieldname in FIELD_LABELS:
        return t(FIELD_LABELS[fieldname])
    base = str(fieldname or "")
    if base.startswith("base_") and base[5:] in FIELD_LABELS:
        return t("{0} (company currency)", t(FIELD_LABELS[base[5:]]))
    if doctype:
        try:
            import frappe

            field = frappe.get_meta(doctype).get_field(fieldname)
            if field and field.label:
                return translate(field.label)
        except Exception:
            # Cosmetic: the humanized fieldname is the label.
            pass
    return humanize(fieldname)


def doctype_label(doctype):
    from ai_agent.presentation.i18n import t

    if doctype in DOCTYPE_LABELS:
        return t(doctype)
    return translate(doctype) if doctype else ""


def plural_label(doctype, default=None):
    """The plural business name of a DocType (e.g. "Sales Invoices"), or `default` (English) translated."""
    from ai_agent.presentation.i18n import t

    if doctype in DOCTYPE_PLURALS:
        return t(DOCTYPE_PLURALS[doctype])
    return t(default) if default else None


def status_display(value):
    from ai_agent.presentation.i18n import t

    text = str(value or "").strip()
    label, tone = STATUS.get(text.lower(), (text, "neutral"))
    return (t(label) if text.lower() in STATUS else label), tone


def looks_like_money(fieldname):
    key = str(fieldname or "").lower()
    if any(hint in key for hint in NOT_MONEY):
        return False
    return any(hint in key for hint in MONEY_HINTS)
