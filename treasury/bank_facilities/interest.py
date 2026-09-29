"""Interest accrual of facility drawdowns (Treasury Company Settings `accrue_facility_interest`).

The job posts, for every submitted drawdown and every month that has ended, one Journal Entry dated the month
end: Dr Interest Expense / Cr the facility's Accrued Interest Account (event `interest_accrual:YYYY-MM`, so a
second run finds it and posts nothing).

Amount of a month = interest earned up to the month end - interest already recognised, never negative:
- earned up to D = sum over the schedule rows of row interest x elapsed days of its period up to D / days of the
  period (day-count interest accrues day by day, a Murabaha profit straight-line over its period);
- recognised up to D = the accruals already posted up to D + the interest the repayments dated up to D
  booked straight to Interest Expense (the part not covered by accrued interest).
Computing it cumulatively keeps the accruals exact (no rounding drift), catches up missed months (every month
end since the last processed one, oldest first) and never books twice the interest a repayment already
expensed. Repayments clear the accrued interest first (`accrued_balance`), whether accrual is on or not.

Overdraft interest is not accrued: the bank charges it on the statement (book it from the bank statement).
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate, today

from treasury.accounting import posting
from treasury.bank_facilities import accounting
from treasury.bank_facilities.utilisation import DRAWDOWN, REPAYMENT, get_source_balances
from treasury.utils.dates import month_ends_between

SAVEPOINT = "treasury_facility_accrual"


def earned_interest(drawdown, upto) -> float:
	"""Interest / profit of the schedule earned from the drawdown date up to and including `upto`."""
	upto = getdate(upto)
	precision = drawdown.precision("amount")
	total, previous = 0.0, getdate(drawdown.drawdown_date)
	for row in sorted(
		(r for r in drawdown.repayment_schedule if not r.is_prepayment), key=lambda r: getdate(r.due_date)
	):
		due_date = getdate(row.due_date)
		days = date_diff(due_date, previous)
		elapsed = min(max(date_diff(min(due_date, upto), previous), 0), max(days, 0))
		if days > 0:
			total += flt(row.interest) * elapsed / days
		elif upto >= due_date:
			total += flt(row.interest)
		previous = due_date
	return flt(total, precision)


def accrual_accounts(drawdown) -> list[str]:
	"""The accrued interest accounts of the drawdown: the ones its accrual Journal Entries credited (whatever the
	facility's account is now) and the facility's current one."""
	gle = frappe.qb.DocType("GL Entry")
	je = frappe.qb.DocType("Journal Entry")
	credited = (
		frappe.qb.from_(gle)
		.join(je)
		.on(je.name == gle.voucher_no)
		.select(gle.account)
		.distinct()
		.where(
			(gle.voucher_type == "Journal Entry")
			& (gle.is_cancelled == 0)
			& (gle.credit_in_account_currency > 0)
			& (je.treasury_source_doctype == DRAWDOWN)
			& (je.treasury_source_name == drawdown.name)
			& (je.treasury_event.like(f"{accounting.EVENT_ACCRUAL_PREFIX}%"))
		)
		.run(pluck=True)
	)
	current = frappe.db.get_value("Bank Facility", drawdown.facility, "accrued_interest_account")
	return sorted(set(credited) | ({current} if current else set()))


def posted_accruals(drawdown, upto=None) -> float:
	"""Accrued interest credited by the accrual Journal Entries of the drawdown up to `upto` (read by source and
	event, on the accounts they credited)."""
	accounts = accrual_accounts(drawdown)
	balances = get_source_balances(
		[(DRAWDOWN, drawdown.name)],
		accounts,
		upto,
		credit_minus_debit=True,
		event_prefix=accounting.EVENT_ACCRUAL_PREFIX,
	)
	return flt(sum(balances.values()))


def expensed_by_repayments(drawdown_name: str, upto) -> float:
	"""Interest the submitted repayments dated up to `upto` booked straight to Interest Expense."""
	rows = frappe.get_all(
		REPAYMENT,
		filters={"drawdown": drawdown_name, "docstatus": 1, "repayment_date": ["<=", getdate(upto)]},
		pluck="interest_expensed",
	)
	return flt(sum(flt(r) for r in rows))


def accrued_balance(drawdown, upto) -> float:
	"""Accrued interest of the drawdown not yet cleared by its repayments, up to `upto` (facility currency)."""
	accounts = accrual_accounts(drawdown)
	if not accounts:
		return 0.0
	sources = [(DRAWDOWN, drawdown.name)] + [
		(REPAYMENT, name)
		for name in frappe.get_all(
			REPAYMENT, filters={"drawdown": drawdown.name, "docstatus": 1}, pluck="name"
		)
	]
	balances = get_source_balances(sources, accounts, upto, credit_minus_debit=True)
	return max(flt(sum(balances.values()), drawdown.precision("amount")), 0.0)


def accrual_amount(drawdown, month_end) -> float:
	"""The accrual of the month ending `month_end` (see module docstring)."""
	precision = drawdown.precision("amount")
	recognised = posted_accruals(drawdown, month_end) + expensed_by_repayments(drawdown.name, month_end)
	return flt(max(earned_interest(drawdown, month_end) - recognised, 0), precision)


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


def post_interest_accruals(as_of=None) -> dict:
	"""Accrue the interest of every month ended up to `as_of` (default today) for the drawdowns of the companies
	with `accrue_facility_interest`. Each drawdown runs in its own savepoint and is committed on its own; a
	failing one is rolled back and logged (once per drawdown and day), the others go on.

	Returns {"posted": [Journal Entries], "failed": [drawdowns]}."""
	as_of = getdate(as_of or today())
	result = {"posted": [], "failed": []}
	companies = frappe.get_all(
		"Treasury Company Settings", filters={"accrue_facility_interest": 1}, pluck="company"
	)
	if not companies:
		return result
	for name in frappe.get_all(
		DRAWDOWN,
		filters={"company": ["in", companies], "docstatus": 1, "drawdown_date": ["<=", as_of]},
		order_by="drawdown_date asc, name asc",
		pluck="name",
	):
		frappe.db.savepoint(SAVEPOINT)
		try:
			result["posted"] += accrue_drawdown(name, as_of)
		except Exception:
			frappe.db.rollback(save_point=SAVEPOINT)
			frappe.clear_messages()
			_log_failure(name)
			result["failed"].append(name)
		else:
			frappe.db.release_savepoint(SAVEPOINT)
		_commit()
	return result


def accrue_drawdown(name: str, as_of) -> list[str]:
	"""Post the missing month-end accruals of one drawdown up to `as_of`; returns the Journal Entries posted."""
	frappe.db.get_value(DRAWDOWN, name, "name", for_update=True)
	drawdown = frappe.get_doc(DRAWDOWN, name)
	facility = frappe.get_doc("Bank Facility", drawdown.facility)
	start = getdate(drawdown.last_accrual_date or drawdown.drawdown_date)
	posted = []
	for month_end in month_ends_between(start, as_of):
		event = accounting.accrual_event(month_end)
		if not posting.find_voucher("Journal Entry", DRAWDOWN, name, event):
			amount = accrual_amount(drawdown, month_end)
			if amount > 0:
				rate = posting.get_cheque_exchange_rate(drawdown.currency, drawdown.company, month_end)
				posted.append(accounting.post_accrual(drawdown, facility, month_end, amount, rate))
		drawdown.db_set("last_accrual_date", month_end, update_modified=False)
	return posted


def _commit():
	"""Keep each drawdown's result even if the job dies later (tests run in one rolled back transaction)."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: allowed in scheduler jobs


def _log_failure(name: str):
	"""Error Log of a failing drawdown, at most once per day (it may fail every day, e.g. closed period)."""
	title = _("Facility interest accrual failed for drawdown {0}").format(name)
	if frappe.db.exists(
		"Error Log",
		{"reference_doctype": DRAWDOWN, "reference_name": name, "method": title, "creation": [">=", today()]},
	):
		return
	frappe.log_error(title=title, reference_doctype=DRAWDOWN, reference_name=name)
