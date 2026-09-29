"""Date helpers shared by the Treasury modules (Cheque Batch installments, facility repayment schedules)."""

from frappe.utils import add_months, get_last_day, getdate


def is_month_end(date) -> bool:
	date = getdate(date)
	return date == getdate(get_last_day(date))


def monthly_dates(first_date, count: int, step_months: int, keep_month_end: bool = False) -> list:
	"""`count` dates every `step_months` months, counted from `first_date` (never from the previous date).

	Counting from the first date keeps the day from drifting (31 Jan, 28 Feb, 31 Mar). With `keep_month_end`, a
	first date on the last day of its month keeps every date on the last day of its month (30 Apr, 31 May,
	30 Jun instead of 30 Apr, 30 May, 30 Jun).
	"""
	first = getdate(first_date)
	month_end = bool(keep_month_end) and is_month_end(first)
	dates = []
	for i in range(count):
		date = getdate(add_months(first, i * step_months))
		dates.append(getdate(get_last_day(date)) if month_end else date)
	return dates


def month_ends_between(after, upto) -> list:
	"""The last days of the months ending after `after` and on or before `upto`, oldest first."""
	after, upto = getdate(after), getdate(upto)
	dates = []
	current = getdate(get_last_day(after))
	if current == after:
		current = getdate(get_last_day(add_months(after, 1)))
	while current <= upto:
		dates.append(current)
		current = getdate(get_last_day(add_months(current, 1)))
	return dates
