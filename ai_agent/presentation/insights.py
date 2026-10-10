"""Analytical findings computed from report rows. Each one is a fact with its evidence and a suggested next step;
nothing here is guessed, and a finding is only produced when the data supports it."""

import frappe

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema
from ai_agent.presentation.i18n import t

NORMAL_SIDE = {"Asset": "debit", "Expense": "debit", "Liability": "credit", "Equity": "credit", "Income": "credit"}
ROOT_NAMES = {"Asset": "asset", "Expense": "expense", "Liability": "liability", "Equity": "equity", "Income": "income"}  # English, t() at use
MAX_PER_KIND = 4
DOMINANT_SHARE = 0.5


def _root_type(account):
    try:
        return frappe.get_cached_value("Account", account, "root_type")
    except Exception:
        return None


def ledger_drill(filters, account=None, party=None):
    """Arguments that open the general ledger for an account / party over the same period, or None."""
    filters = filters or {}
    company = filters.get("company")
    start = filters.get("from_date") or filters.get("period_start_date")
    end = filters.get("to_date") or filters.get("period_end_date") or filters.get("report_date")
    if not start and filters.get("fiscal_year"):
        try:
            start, end = frappe.db.get_value("Fiscal Year", filters["fiscal_year"], ["year_start_date", "year_end_date"])
        except Exception:
            start = None
    if not (company and start and end):
        return None
    ledger = {"company": company, "from_date": str(start), "to_date": str(end), "group_by": "Group by Voucher (Consolidated)"}
    if account:
        ledger["account"] = [account]
    if party:
        ledger["party_type"] = filters.get("party_type") or "Customer"
        ledger["party"] = [party]
    return {"report_name": "General Ledger", "filters": ledger}


def abnormal_balances(rows, filters, currency):
    """Trial balance: accounts whose closing balance sits on the side opposite to their nature."""
    out = []
    for row in rows:
        account = row.get("account")
        if not account or row.get("is_group") or frappe.utils.cint(row.get("indent")) == 0 and row.get("has_value") is False:
            continue
        debit, credit = fmt.to_number(row.get("closing_debit")) or 0, fmt.to_number(row.get("closing_credit")) or 0
        if not (debit or credit):
            continue
        root = _root_type(account)
        normal = NORMAL_SIDE.get(root)
        side = "debit" if debit > credit else "credit"
        if not normal or side == normal:
            continue
        amount = abs(debit - credit)
        drill = ledger_drill(filters, account=account)
        out.append((amount, schema.insight(
            t("Unusual balance: {0}", row.get("account_name") or account),
            t("A {0} account has a closing debit balance of {1}, though it normally carries a credit balance." if side == "debit"
              else "A {0} account has a closing credit balance of {1}, though it normally carries a debit balance.",
              t(ROOT_NAMES[root]), fmt.money(amount, currency)),
            cause=t("A reversing entry that was never settled, or a payment or return posted to this account instead of the right one."),
            impact=t("The account shows with the opposite sign in the financial statements and may distort its group total."),
            action=t("Review the account's entries for the period and find the one causing it."), level="warning",
            drill={"label": t("Ledger entries"), "tool": "reports__run", "arguments": drill} if drill else None)))
    return [item for _, item in sorted(out, key=lambda pair: -pair[0])[:MAX_PER_KIND]]


def opposite_party_balances(rows, filters, currency, payable=False):
    """Receivables: customers we owe (credit balances). Payables: suppliers who owe us (debit balances)."""
    out = []
    for row in rows:
        party = row.get("party") or row.get("customer") or row.get("supplier")
        outstanding = fmt.to_number(row.get("outstanding") if row.get("outstanding") is not None else row.get("outstanding_amount"))
        if not party or outstanding is None or outstanding >= 0:
            continue
        out.append((abs(outstanding), party, row.get("party_name") or row.get("customer_name") or row.get("supplier_name") or party, row.get("voucher_no")))
    if not out:
        return []
    total = sum(a for a, *_ in out)
    parties = sorted({p for _, p, *_ in out})
    top = sorted(out, key=lambda x: -x[0])[:6]
    title = "Debit balances for {0} suppliers" if payable else "Credit balances for {0} customers"
    observation = ("Debit balances total {0}; the largest is {1} at {2}." if payable
                   else "Credit balances total {0}; the largest is {1} at {2}.")
    return [schema.insight(
        t(title, fmt.number(len(parties))),
        t(observation, fmt.money(total, currency), top[0][2], fmt.money(top[0][0], currency)),
        cause=t("An advance or a return not allocated to an invoice, or a payment recorded twice."),
        impact=t("It lowers the total shown as outstanding and may hide amounts that are really due."),
        action=t("Allocate the payments to their invoices, or settle the balance with a journal entry."), level="warning",
        documents=[{"label": f"{name}: {fmt.money(amount, currency)}", "doctype": None, "name": voucher} for amount, _, name, voucher in top])]


def dominant_voucher(rows, currency, label=None):
    """A single document that makes up half or more of the period's movement. `label`: whose movement (default: the account)."""
    out = []
    label = label or t("the account")
    for side, title in (("debit", "One document makes up {0} of the debits"), ("credit", "One document makes up {0} of the credits")):
        values = [(fmt.to_number(r.get(side)) or 0, r) for r in rows if r.get("voucher_no")]
        total = sum(v for v, _ in values)
        if len(values) < 3 or not total:
            continue
        amount, row = max(values, key=lambda pair: pair[0])
        share = amount / total
        if share < DOMINANT_SHARE:
            continue
        out.append(schema.insight(
            t(title, fmt.percent(share, 0)),
            t("Document {0} accounts for {1} of the {2} total movement on {3}.",
              row["voucher_no"], fmt.money(amount, currency), fmt.money(total, currency), label),
            cause=t("A single large transaction, or a summary or opening entry."),
            impact=t("Any mistake in this document changes the whole period's balance."),
            action=t("Check that the document, its attachments and its approval are correct."), level="info",
            documents=[{"label": str(row["voucher_no"]), "doctype": row.get("voucher_type"), "name": row["voucher_no"]}],
            drill={"label": t("Open document"), "tool": "data__get_document", "arguments": {"doctype": row.get("voucher_type"), "name": row["voucher_no"]}} if row.get("voucher_type") else None))
    return out[:1]
