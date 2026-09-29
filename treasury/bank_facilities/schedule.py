"""Repayment schedule arithmetic of a Facility Drawdown (pure functions, no database access).

Periods: installments fall every 1 / 3 / 6 / 12 months (Monthly / Quarterly / Semi-Annually / Annually)
from the first repayment date (default: one period after the drawdown date), counted from the first date so
the day never drifts, and a first date on a month end keeps every date on a month end
(treasury.utils.dates.monthly_dates). Frequency Bullet: one installment at the end of the tenor.

Interest of a row = outstanding principal before the row × rate × days of the period / day-count basis
(Actual/365 or Actual/360), the period running from the previous due date (the drawdown date for the first
row) to the row's due date. Every amount is rounded to the currency precision; the principal of the last row
absorbs the rounding, so the principal always adds up to the drawdown amount exactly.

Methods:
- Equal Principal: the principal is split evenly over the amortising rows.
- Equal Installment (annuity): installment = P·r / (1 − (1 + r)^−n) with r = rate × months per period / 12
  (the periodic nominal rate); each row's principal = installment − its day-count interest, so the rows'
  totals are equal except the last one, which repays whatever principal is left.
- Bullet: the whole principal on the last row; interest on every row.
Grace months: the first rows (grace months / months per period) are interest only.
Profit amount (Murabaha): a fixed profit instead of a rate, spread straight-line over all the rows (the last
row absorbs the rounding); the principal follows Equal Principal (annuity = equal principal + equal profit)
or Bullet.
"""

import frappe
from frappe import _
from frappe.utils import add_months, cint, date_diff, flt, getdate

from treasury.utils.dates import monthly_dates

FREQUENCY_MONTHS = {"Monthly": 1, "Quarterly": 3, "Semi-Annually": 6, "Annually": 12}
BULLET = "Bullet"
EQUAL_PRINCIPAL = "Equal Principal"
ANNUITY = "Equal Installment (annuity)"
METHODS = (EQUAL_PRINCIPAL, ANNUITY, BULLET)
DAY_COUNT_BASIS = {"Actual/365": 365, "Actual/360": 360}


def period_months(frequency: str, tenor_months: int) -> int:
	if frequency == BULLET:
		return cint(tenor_months)
	if frequency not in FREQUENCY_MONTHS:
		frappe.throw(_("Invalid Repayment Frequency: {0}").format(frequency))
	return FREQUENCY_MONTHS[frequency]


def day_count_basis(day_count: str) -> int:
	if day_count not in DAY_COUNT_BASIS:
		frappe.throw(_("Invalid Day Count: {0}").format(day_count))
	return DAY_COUNT_BASIS[day_count]


def period_interest(outstanding: float, rate: float, days: int, basis: int, precision: int) -> float:
	"""Simple interest of one period on `outstanding` (rate in % per annum)."""
	return flt(flt(outstanding) * flt(rate) / 100 * max(cint(days), 0) / basis, precision)


def annuity_installment(principal: float, rate: float, months: int, count: int, precision: int) -> float:
	"""Level installment repaying `principal` in `count` periods of `months` months at `rate` % p.a."""
	periodic = flt(rate) / 100 * months / 12
	if not periodic:
		return flt(flt(principal) / count, precision)
	return flt(flt(principal) * periodic / (1 - (1 + periodic) ** -count), precision)


def split_evenly(total: float, count: int, precision: int) -> list[float]:
	"""`count` equal parts of `total`; the last part absorbs the rounding."""
	if count <= 0:
		return []
	part = flt(flt(total) / count, precision)
	parts = [part] * (count - 1)
	parts.append(flt(flt(total) - sum(parts), precision))
	return parts


def build_schedule(
	*,
	amount: float,
	start_date,
	tenor_months: int,
	frequency: str,
	method: str,
	first_repayment_date=None,
	grace_months: int = 0,
	rate: float = 0,
	day_count: str = "Actual/365",
	profit_amount: float = 0,
	precision: int = 2,
) -> list[frappe._dict]:
	"""Rows {installment_no, due_date, principal, interest, total, outstanding_after} of a new drawdown."""
	amount = flt(amount, precision)
	tenor_months, grace_months = cint(tenor_months), cint(grace_months)
	if amount <= 0:
		frappe.throw(_("Amount must be greater than zero."))
	if tenor_months <= 0:
		frappe.throw(_("Tenor (Months) must be greater than zero."))
	if method not in METHODS:
		frappe.throw(_("Invalid Repayment Method: {0}").format(method))
	if flt(rate) < 0 or flt(profit_amount) < 0:
		frappe.throw(_("The interest rate and the profit amount cannot be negative."))
	months = period_months(frequency, tenor_months)
	if tenor_months % months:
		frappe.throw(
			_("Tenor (Months) {0} must be a multiple of the {1} months of a {2} period.").format(
				tenor_months, months, _(frequency)
			)
		)
	if grace_months < 0 or grace_months % months:
		frappe.throw(
			_("Grace (Months) must be a multiple of the {0} months of a {1} period.").format(months, _(frequency))
		)
	count = tenor_months // months
	grace = grace_months // months
	if grace >= count:
		frappe.throw(_("The grace period must end before the last installment."))

	start = getdate(start_date)
	first = getdate(first_repayment_date) if first_repayment_date else getdate(add_months(start, months))
	if first <= start:
		frappe.throw(_("The First Repayment Date must be after the drawdown date."))
	dates = monthly_dates(first, count, months, keep_month_end=True)

	basis = day_count_basis(day_count)
	profit = flt(profit_amount, precision)
	profits = split_evenly(profit, count, precision) if profit else None
	amortising = count - grace
	if method == BULLET:
		principals = [0.0] * (amortising - 1) + [amount]
	elif method == EQUAL_PRINCIPAL or profits:
		principals = split_evenly(amount, amortising, precision)
	else:
		principals = None  # annuity: from the installment, row by row
	installment = (
		annuity_installment(amount, rate, months, amortising, precision) if principals is None else 0
	)

	rows, outstanding, previous = [], amount, start
	for index, due_date in enumerate(dates):
		interest = (
			profits[index]
			if profits
			else period_interest(outstanding, rate, date_diff(due_date, previous), basis, precision)
		)
		if index < grace:
			principal = 0.0
		elif index == count - 1:
			principal = outstanding
		elif principals is not None:
			principal = principals[index - grace]
		else:
			principal = flt(installment - interest, precision)
			if principal < 0:
				frappe.throw(
					_("The interest of installment {0} exceeds the installment {1}.").format(index + 1, installment)
				)
			principal = min(principal, outstanding)
		outstanding = flt(outstanding - principal, precision)
		rows.append(
			frappe._dict(
				installment_no=index + 1,
				due_date=due_date,
				principal=flt(principal, precision),
				interest=interest,
				total=flt(principal + interest, precision),
				outstanding_after=outstanding,
			)
		)
		previous = due_date
	return rows
