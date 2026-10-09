"""Analytical findings computed from report rows. Each one is a fact with its evidence and a suggested next step;
nothing here is guessed, and a finding is only produced when the data supports it."""

import frappe

from ai_agent.presentation import formatters as fmt
from ai_agent.presentation import schema

NORMAL_SIDE = {"Asset": "debit", "Expense": "debit", "Liability": "credit", "Equity": "credit", "Income": "credit"}
ROOT_AR = {"Asset": "أصول", "Expense": "مصروفات", "Liability": "التزامات", "Equity": "حقوق ملكية", "Income": "إيرادات"}
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
            f"رصيد غير معتاد: {row.get('account_name') or account}",
            f"حساب من نوع {ROOT_AR.get(root, root)} رصيده الختامي {'مدين' if side == 'debit' else 'دائن'} بمبلغ {fmt.money(amount, currency)}، وطبيعته أن يكون {'مدينًا' if normal == 'debit' else 'دائنًا'}.",
            cause="قيد عكسي لم يُسوَّ، أو دفعة أو مرتجع سُجّل على هذا الحساب بدل حسابه الصحيح.",
            impact="يظهر الحساب في القوائم المالية بإشارة معاكسة، وقد يشوّه إجمالي مجموعته.",
            action="راجع قيود الحساب في الفترة وحدّد القيد المسبِّب.", level="warning",
            drill={"label": "قيود الأستاذ", "tool": "reports__run", "arguments": drill} if drill else None)))
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
    noun, side = ("موردين", "مدينة") if payable else ("عملاء", "دائنة")
    return [schema.insight(
        f"أرصدة {side} لدى {fmt.number(len(parties))} من ال{noun}",
        f"إجمالي الأرصدة ال{side} {fmt.money(total, currency)}؛ أعلاها {top[0][2]} بمبلغ {fmt.money(top[0][0], currency)}.",
        cause="دفعة مقدمة أو مرتجع لم يُخصَّص على فاتورة، أو دفعة مسجلة مرتين.",
        impact="تُنقص إجمالي الذمم الظاهر، وقد تخفي مبالغ مستحقة فعلًا.",
        action="خصّص الدفعات على فواتيرها أو سوِّ الرصيد بقيد.", level="warning",
        documents=[{"label": f"{name}: {fmt.money(amount, currency)}", "doctype": None, "name": voucher} for amount, _, name, voucher in top])]


def dominant_voucher(rows, currency, label="الحساب"):
    """A single document that makes up half or more of the period's movement."""
    out = []
    for side, word in (("debit", "المدين"), ("credit", "الدائن")):
        values = [(fmt.to_number(r.get(side)) or 0, r) for r in rows if r.get("voucher_no")]
        total = sum(v for v, _ in values)
        if len(values) < 3 or not total:
            continue
        amount, row = max(values, key=lambda pair: pair[0])
        share = amount / total
        if share < DOMINANT_SHARE:
            continue
        out.append(schema.insight(
            f"مستند واحد يمثل {fmt.percent(share, 0)} من {word}",
            f"المستند {row['voucher_no']} بمبلغ {fmt.money(amount, currency)} من إجمالي {fmt.money(total, currency)} في حركة {label}.",
            cause="عملية كبيرة منفردة، أو قيد تجميعي أو افتتاحي.",
            impact="أي خطأ في هذا المستند يغيّر رصيد الفترة كله.",
            action="تأكد من صحة المستند ومرفقاته واعتماده.", level="info",
            documents=[{"label": str(row["voucher_no"]), "doctype": row.get("voucher_type"), "name": row["voucher_no"]}],
            drill={"label": "فتح المستند", "tool": "data__get_document", "arguments": {"doctype": row.get("voucher_type"), "name": row["voucher_no"]}} if row.get("voucher_type") else None))
    return out[:1]
