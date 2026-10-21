"""Which currency an amount is in. ERPNext declares it per field (the Currency field's `options`), so the company
currency is never printed on a transaction amount, nor the other way round."""

import frappe

COMPANY_OPTION = "Company:company:default_currency"


def company_currency(company):
    if not company:
        return None
    try:
        return frappe.get_cached_value("Company", company, "default_currency")
    except Exception:
        # Cosmetic: the amount is shown without a currency.
        return None


def field_currency(field, doc, parent=None):
    """Currency of one Currency field of `doc` (a dict). `parent` is the parent document for child rows."""
    options = (getattr(field, "options", None) or "").strip()
    owner = parent if parent is not None else doc
    company = (owner or {}).get("company") or (doc or {}).get("company")
    if options.startswith("Company:"):
        return company_currency(company)
    if options:
        # `options` names the field that holds the currency, on the row first, then on the parent document.
        for source in (doc, parent):
            if source and source.get(options):
                return source.get(options)
    if field is not None and str(getattr(field, "fieldname", "")).startswith("base_"):
        return company_currency(company)
    return (owner or {}).get("currency") or (doc or {}).get("currency") or company_currency(company)


def is_multi_currency(doc):
    base = company_currency((doc or {}).get("company"))
    currency = (doc or {}).get("currency") or (doc or {}).get("paid_from_account_currency")
    return bool(base and currency and base != currency)


def exchange_rate_text(doc):
    """'1 USD = 530 YER' for a document in a foreign currency, else None."""
    from ai_agent.presentation import formatters as fmt

    if not is_multi_currency(doc):
        return None
    rate = fmt.to_number(doc.get("conversion_rate") or doc.get("source_exchange_rate"))
    if not rate:
        return None
    base = company_currency(doc.get("company"))
    return f"1 {doc.get('currency') or doc.get('paid_from_account_currency')} = {fmt.number(rate, 6 if rate < 1 else None).rstrip('0').rstrip('.') if rate < 1 else fmt.number(rate)} {base}"
