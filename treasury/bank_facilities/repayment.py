"""Facility Repayment service: allocation to the repayment schedule, prepayment reschedule, row status.

Allocation (a repayment carries a principal and an interest amount):
- Oldest First: the interest to the unpaid interest of the rows, oldest due date first; the principal the same
  way. With Prepayment, the principal goes first to the rows due up to the repayment date and the rest is a
  prepayment (see below); without it, it may settle future installments early, oldest first.
- Selected Row: both amounts to one installment; with Prepayment the principal beyond its due principal is a
  prepayment.
Nothing may be allocated beyond what is still unpaid; an amount that cannot be allocated is refused.

Prepayment (extra principal): recorded as a paid schedule row of its own (`is_prepayment`, dated the repayment
date), so the schedule's principal still adds up to the drawdown amount, and the remaining installments are
rescheduled by **reducing the installment amount** (the tenor is kept; reducing the tenor instead is not
implemented). Rescheduled: the untouched rows (nothing paid) due after the repayment date. Their principal
is spread again by the drawdown's method (Equal Principal: evenly; annuity: level installments over the
remaining rows; Bullet: all on the last row; interest-only grace rows stay interest-only), and their interest
is recomputed on the new outstanding principal with the drawdown's day count, from the previous due date (the
whole current period is charged on the reduced principal). A Murabaha profit (fixed profit amount) is not
recomputed: only the principal is spread again.

Cancel: only the latest submitted repayment of a drawdown may be cancelled; the schedule is restored from the
snapshot the repayment took before changing it, and its prepayment row is removed.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, today

from treasury.bank_facilities import schedule

DRAWDOWN = "Facility Drawdown"
SCHEDULE = "Facility Repayment Schedule"
OLDEST_FIRST = "Oldest First"
SELECTED_ROW = "Selected Row"
PENDING, PARTIALLY_PAID, PAID, OVERDUE = "Pending", "Partially Paid", "Paid", "Overdue"
SNAPSHOT_FIELDS = (
	"principal",
	"interest",
	"total",
	"outstanding_after",
	"paid_principal",
	"paid_interest",
	"status",
	"payment_journal",
	"repayment",
)


# ---------------------------------------------------------------------------
# Row status
# ---------------------------------------------------------------------------


def unpaid_principal(row) -> float:
	return max(flt(row.principal) - flt(row.paid_principal), 0)


def unpaid_interest(row) -> float:
	return max(flt(row.interest) - flt(row.paid_interest), 0)


def row_status(row, as_of=None) -> str:
	"""Paid / Overdue / Partially Paid / Pending of a schedule row on `as_of` (default today)."""
	if flt(row.paid_principal) + flt(row.paid_interest) >= flt(row.total) - 0.005:
		return PAID
	if getdate(row.due_date) < getdate(as_of or today()):
		return OVERDUE
	if flt(row.paid_principal) or flt(row.paid_interest):
		return PARTIALLY_PAID
	return PENDING


def refresh_repayment_status(drawdown_name: str, as_of=None):
	"""Store the row statuses (Overdue moves with the calendar) and the drawdown's Repaid / Open status."""
	rows = frappe.get_all(
		SCHEDULE,
		filters={"parenttype": DRAWDOWN, "parent": drawdown_name},
		fields=["name", "due_date", "total", "paid_principal", "paid_interest", "status"],
	)
	for row in rows:
		status = row_status(row, as_of)
		if status != row.status:
			frappe.db.set_value(SCHEDULE, row.name, "status", status, update_modified=False)
	repaid = bool(rows) and all(row_status(r, as_of) == PAID for r in rows)
	frappe.db.set_value(
		DRAWDOWN, drawdown_name, "repayment_status", "Repaid" if repaid else "Open", update_modified=False
	)


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def allocate(drawdown, repayment) -> frappe._dict:
	"""Split the repayment over the schedule rows (nothing is changed). Returns {rows: [{row, principal,
	interest}], prepayment}."""
	precision = drawdown.precision("amount")
	principal = flt(repayment.principal_paid, precision)
	interest = flt(repayment.interest_paid, precision)
	date = getdate(repayment.repayment_date)
	rows = sorted(
		(r for r in drawdown.repayment_schedule if not r.is_prepayment),
		key=lambda r: (getdate(r.due_date), cint(r.installment_no)),
	)
	allocations: dict[str, frappe._dict] = {}

	def take(row, fieldname, wanted, available):
		amount = flt(min(wanted, available), precision)
		if amount > 0:
			entry = allocations.setdefault(row.name, frappe._dict(row=row, principal=0.0, interest=0.0))
			entry[fieldname] = flt(entry[fieldname] + amount, precision)
		return flt(wanted - max(amount, 0), precision)

	if repayment.allocation_mode == SELECTED_ROW:
		row = next((r for r in rows if cint(r.installment_no) == cint(repayment.installment_no)), None)
		if not row:
			frappe.throw(_("Installment {0} does not exist on drawdown {1}.").format(
				repayment.installment_no, frappe.bold(drawdown.name)
			))
		interest = take(row, "interest", interest, unpaid_interest(row))
		principal = take(row, "principal", principal, unpaid_principal(row))
	else:
		for row in rows:
			interest = take(row, "interest", interest, unpaid_interest(row))
		for row in rows:
			if repayment.prepayment and getdate(row.due_date) > date:
				break
			principal = take(row, "principal", principal, unpaid_principal(row))

	if interest > 0:
		frappe.throw(
			_("The interest paid exceeds the unpaid interest of the allocated installments by {0}.").format(
				interest
			)
		)
	prepayment = 0.0
	if principal > 0:
		if not repayment.prepayment:
			frappe.throw(
				_(
					"The principal paid exceeds the unpaid principal of the allocated installments by {0}. Tick Prepayment to prepay it."
				).format(principal)
			)
		prepayment = principal
		# the installments this repayment pays something on are not rescheduled
		reschedulable = sum(
			flt(r.principal) for r in reschedulable_rows(drawdown, date) if r.name not in allocations
		)
		if prepayment > flt(reschedulable, precision):
			frappe.throw(
				_(
					"The prepayment {0} exceeds the principal {1} of the unpaid installments due after the repayment date."
				).format(prepayment, flt(reschedulable, precision))
			)
	return frappe._dict(rows=list(allocations.values()), prepayment=flt(prepayment, precision))


def reschedulable_rows(drawdown, date) -> list:
	"""Untouched installments (nothing paid) due after `date`, in date order."""
	return sorted(
		(
			r
			for r in drawdown.repayment_schedule
			if not r.is_prepayment
			and getdate(r.due_date) > getdate(date)
			and not flt(r.paid_principal)
			and not flt(r.paid_interest)
		),
		key=lambda r: getdate(r.due_date),
	)


# ---------------------------------------------------------------------------
# Apply / undo on submit / cancel
# ---------------------------------------------------------------------------


def snapshot(drawdown) -> str:
	return json.dumps(
		[{"name": r.name, **{f: r.get(f) for f in SNAPSHOT_FIELDS}} for r in drawdown.repayment_schedule],
		default=str,
	)


def apply_repayment(drawdown, repayment, journal: str):
	"""Record the allocation of a submitted repayment on the schedule rows (and the prepayment reschedule)."""
	precision = drawdown.precision("amount")
	rows = {r.name: r for r in drawdown.repayment_schedule}
	for allocation in repayment.allocations:
		row = rows.get(allocation.schedule_row)
		if not row:
			frappe.throw(_("Schedule row {0} no longer exists.").format(allocation.schedule_row))
		row.paid_principal = flt(flt(row.paid_principal) + flt(allocation.principal), precision)
		row.paid_interest = flt(flt(row.paid_interest) + flt(allocation.interest), precision)
		row.payment_journal = journal
		row.repayment = repayment.name
	if flt(repayment.prepayment_amount):
		reschedule(drawdown, repayment.repayment_date, flt(repayment.prepayment_amount))
		drawdown.append(
			"repayment_schedule",
			{
				"installment_no": 0,
				"due_date": getdate(repayment.repayment_date),
				"principal": flt(repayment.prepayment_amount),
				"interest": 0,
				"total": flt(repayment.prepayment_amount),
				"paid_principal": flt(repayment.prepayment_amount),
				"paid_interest": 0,
				"is_prepayment": 1,
				"payment_journal": journal,
				"repayment": repayment.name,
			},
		)
	_set_outstanding_after(drawdown)
	_save_rows(drawdown)
	refresh_repayment_status(drawdown.name)


def check_no_later_repayment(drawdown_name: str, repayment):
	"""Only the latest submitted repayment of a drawdown may be cancelled."""
	later = frappe.get_all(
		"Facility Repayment",
		filters={
			"drawdown": drawdown_name,
			"docstatus": 1,
			"name": ["!=", repayment.name],
			"creation": [">", repayment.creation],
		},
		pluck="name",
	)
	if later:
		frappe.throw(
			_("Cancel the later repayments of drawdown {0} first: {1}.").format(
				frappe.bold(drawdown_name), ", ".join(frappe.bold(r) for r in later)
			),
			title=_("Not Allowed"),
		)


def undo_repayment(drawdown, repayment):
	"""Restore the schedule rows as they were before the repayment (its snapshot)."""
	check_no_later_repayment(drawdown.name, repayment)
	saved = {r["name"]: r for r in json.loads(repayment.schedule_snapshot or "[]")}
	for row in list(drawdown.repayment_schedule):
		if row.is_prepayment and row.repayment == repayment.name:
			frappe.db.delete(SCHEDULE, {"name": row.name})
			drawdown.repayment_schedule.remove(row)
			continue
		if row.name in saved:
			for fieldname in SNAPSHOT_FIELDS:
				row.set(fieldname, saved[row.name].get(fieldname))
	_save_rows(drawdown)
	refresh_repayment_status(drawdown.name)


def _save_rows(drawdown):
	"""Write the schedule rows of a submitted drawdown (only the repayments change them)."""
	for index, row in enumerate(
		sorted(drawdown.repayment_schedule, key=lambda r: (getdate(r.due_date), -cint(r.is_prepayment))), 1
	):
		row.idx = index
		row.total = flt(flt(row.principal) + flt(row.interest), drawdown.precision("amount"))
		if row.get("__islocal") or not row.name or not frappe.db.exists(SCHEDULE, row.name):
			row.parent, row.parenttype, row.parentfield = drawdown.name, DRAWDOWN, "repayment_schedule"
			row.docstatus = drawdown.docstatus
			row.db_insert()
		else:
			row.db_update()


def _set_outstanding_after(drawdown):
	precision = drawdown.precision("amount")
	outstanding = flt(drawdown.amount)
	for row in sorted(
		drawdown.repayment_schedule, key=lambda r: (getdate(r.due_date), -cint(r.is_prepayment))
	):
		outstanding = flt(outstanding - flt(row.principal), precision)
		row.outstanding_after = outstanding


def reschedule(drawdown, date, prepayment: float):
	"""Reduce the installments due after `date` for a prepayment of `prepayment` (see module docstring)."""
	precision = drawdown.precision("amount")
	rows = reschedulable_rows(drawdown, date)
	amortising = [r for r in rows if flt(r.principal) > 0]
	remaining = flt(sum(flt(r.principal) for r in amortising) - prepayment, precision)
	if not amortising or remaining < 0:
		frappe.throw(
			_("The prepayment exceeds the principal of the unpaid installments due after the repayment date.")
		)
	fixed_profit = bool(flt(drawdown.profit_amount))
	basis = schedule.day_count_basis(drawdown.day_count or "Actual/365")

	if drawdown.repayment_method == schedule.BULLET:
		principals = [0.0] * (len(amortising) - 1) + [remaining]
	elif drawdown.repayment_method == schedule.EQUAL_PRINCIPAL or fixed_profit:
		principals = schedule.split_evenly(remaining, len(amortising), precision)
	else:
		principals = None
	months = schedule.period_months(drawdown.repayment_frequency, drawdown.tenor_months)
	installment = (
		schedule.annuity_installment(remaining, drawdown.interest_rate, months, len(amortising), precision)
		if principals is None
		else 0
	)

	previous = _previous_due_date(drawdown, rows[0])
	left, amortised = remaining, 0
	for row in rows:
		if not fixed_profit:
			row.interest = schedule.period_interest(
				_principal_before(drawdown, row, prepayment),
				drawdown.interest_rate,
				date_diff(row.due_date, previous),
				basis,
				precision,
			)
		if flt(row.principal) > 0:
			amortised += 1
			if amortised == len(amortising):
				principal = left
			elif principals is not None:
				principal = principals[amortised - 1]
			else:
				principal = min(max(flt(installment - row.interest, precision), 0), left)
			row.principal = flt(principal, precision)
			left = flt(left - row.principal, precision)
		row.total = flt(flt(row.principal) + flt(row.interest), precision)
		previous = getdate(row.due_date)


def _principal_before(drawdown, row, prepayment: float) -> float:
	"""Scheduled principal outstanding just before `row`: the amount less the new prepayment and the principal
	of every earlier row (earlier prepayment rows and the rows already rescheduled included)."""
	earlier = sum(
		flt(r.principal) for r in drawdown.repayment_schedule if getdate(r.due_date) < getdate(row.due_date)
	)
	return max(flt(drawdown.amount - prepayment - earlier, drawdown.precision("amount")), 0)


def _previous_due_date(drawdown, first_row):
	earlier = [
		getdate(r.due_date)
		for r in drawdown.repayment_schedule
		if not r.is_prepayment and getdate(r.due_date) < getdate(first_row.due_date)
	]
	return max(earlier) if earlier else getdate(drawdown.drawdown_date)
