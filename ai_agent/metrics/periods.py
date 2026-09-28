"""Named periods for metrics, resolved in the site's time zone.

"Year" always means the company's fiscal year; quarters are calendar
quarters. Every resolved period carries its concrete dates so the answer can
state exactly what was measured.
"""

from datetime import timedelta

import frappe
from frappe.utils import add_days, add_months, add_years, get_first_day, get_last_day, getdate, now_datetime

NAMED_PERIODS = (
	"today", "yesterday", "this_week", "last_week", "this_month", "last_month",
	"this_quarter", "last_quarter", "this_fiscal_year", "last_fiscal_year", "previous_fiscal_year",
)
COMPARE_MODES = ("previous_period", "same_period_last_year", "yesterday_same_time")


class PeriodError(frappe.ValidationError):
	pass


def resolve_period(period, company=None, now=None):
	"""Return {"key", "from", "to", "until_time"} for a named period or {"from", "to"}."""
	now = now or now_datetime()
	today = getdate(now)
	if isinstance(period, dict):
		start, end = period.get("from"), period.get("to")
		if not start or not end:
			raise PeriodError("A custom period needs both 'from' and 'to' dates.")
		start, end = getdate(start), getdate(end)
		if start > end:
			raise PeriodError("Period 'from' is after 'to'.")
		return _span("custom", start, end)

	key = str(period or "this_month").strip().lower()
	if key == "today":
		return {"key": key, "from": today, "to": today, "until_time": now.time().replace(microsecond=0)}
	if key == "yesterday":
		day = add_days(today, -1)
		return _span(key, day, day)
	if key == "this_week":
		return _span(key, today - timedelta(days=_weekday_offset(today)), today)
	if key == "last_week":
		start = today - timedelta(days=_weekday_offset(today) + 7)
		return _span(key, start, add_days(start, 6))
	if key == "this_month":
		return _span(key, get_first_day(today), today)
	if key == "last_month":
		start = get_first_day(add_months(today, -1))
		return _span(key, start, get_last_day(start))
	if key == "this_quarter":
		return _span(key, _quarter_start(today), today)
	if key == "last_quarter":
		start = add_months(_quarter_start(today), -3)
		return _span(key, start, get_last_day(add_months(start, 2)))
	if key in ("this_year", "this_fiscal_year"):
		start, end = fiscal_year(today, company)
		return _span("this_fiscal_year", start, min(end, today))
	if key in ("last_year", "last_fiscal_year"):
		start, _ = fiscal_year(today, company)
		return _span("last_fiscal_year", *fiscal_year(add_days(start, -1), company))
	if key == "previous_fiscal_year":
		start, _ = fiscal_year(today, company)
		last_start, _ = fiscal_year(add_days(start, -1), company)
		return _span(key, *fiscal_year(add_days(last_start, -1), company))
	raise PeriodError(f"Unknown period '{period}'. Use one of: {', '.join(NAMED_PERIODS)} or {{from, to}}.")


def comparison_period(base, mode, company=None):
	"""The period to compare ``base`` against."""
	if mode == "yesterday_same_time":
		day = add_days(base["from"], -1)
		return {"key": mode, "from": day, "to": day, "until_time": base.get("until_time")}
	if mode == "same_period_last_year":
		return {**_span(mode, add_years(base["from"], -1), add_years(base["to"], -1)), "until_time": base.get("until_time")}
	if mode == "previous_period":
		key = base.get("key")
		# Whole calendar/fiscal units compare with the previous whole unit
		# (to-date units with the same number of elapsed days).
		if key in ("this_month", "last_month"):
			start = get_first_day(add_months(base["from"], -1))
			end = get_last_day(start) if key == "last_month" else min(get_last_day(start), add_months(base["to"], -1))
			return _span(mode, start, end)
		if key in ("this_fiscal_year", "last_fiscal_year"):
			start, end = fiscal_year(add_days(base["from"], -1), company)
			if key == "this_fiscal_year":
				end = min(end, add_days(start, (getdate(base["to"]) - getdate(base["from"])).days))
			return _span(mode, start, end)
		length = (getdate(base["to"]) - getdate(base["from"])).days + 1
		end = add_days(base["from"], -1)
		return {**_span(mode, add_days(end, -(length - 1)), end), "until_time": base.get("until_time")}
	raise PeriodError(f"Unknown compare mode '{mode}'. Use one of: {', '.join(COMPARE_MODES)}.")


def describe(period):
	text = str(period["from"]) if period["from"] == period["to"] else f"{period['from']} → {period['to']}"
	if period.get("until_time"):
		text += f" (حتى الساعة {period['until_time']})"
	return text


def fiscal_year(day, company=None):
	try:
		from erpnext.accounts.utils import get_fiscal_year

		_, start, end = get_fiscal_year(day, company=company, verbose=0)
		return getdate(start), getdate(end)
	except Exception:
		# No Fiscal Year record covers this date: fall back to the calendar year.
		return getdate(f"{day.year}-01-01"), getdate(f"{day.year}-12-31")


def _span(key, start, end):
	return {"key": key, "from": getdate(start), "to": getdate(end), "until_time": None}


def _weekday_offset(day):
	"""Days since the week start configured in System Settings (default Monday)."""
	first = frappe.db.get_single_value("System Settings", "first_day_of_the_week") or "Monday"
	names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
	return (day.weekday() - names.index(first)) % 7 if first in names else day.weekday()


def _quarter_start(day):
	return getdate(f"{day.year}-{(day.month - 1) // 3 * 3 + 1:02d}-01")
