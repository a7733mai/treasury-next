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

Cancel: only the last submitted repayment of a drawdown may be cancelled (the highest `submission_seq`, stamped
at submit under the drawdown lock: submission order, not creation order, since drafts may be submitted in any
order); the schedule is restored from the snapshot the repayment took before changing it, and its prepayment
row is removed.

Installment cheques (PDC to Lender, spec 7g §3.5): a row with a live cheque is paid only by that cheque (I1:
Oldest First skips it, Selected Row refuses it, it is never rescheduled, no prepayment while later rows have
live cheques). A cheque repayment (settlement "PDC Cheque": one Selected Row, exactly the row's unpaid amount,
no prepayment or penalty, I2) may be cancelled at any time, by the cheque's bounce / replacement: it is undone
by subtracting its own allocations (delta undo); the snapshot of any other repayment never overwrites rows paid
by a cheque (I3). Istisna'a: no principal beyond what was disbursed and not yet repaid.

Concurrency: submit and cancel lock the drawdown row (`lock_drawdown`) and every schedule change increments
the drawdown's `schedule_version`, so a repayment never acts on schedule rows read before another repayment
committed.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, date_diff, flt, getdate, today

from treasury.bank_facilities import schedule

DRAWDOWN = "Facility Drawdown"
REPAYMENT = "Facility Repayment"
SCHEDULE = "Facility Repayment Schedule"
OLDEST_FIRST = "Oldest First"
SELECTED_ROW = "Selected Row"
SETTLED_BY_CHEQUE = "PDC Cheque"
ISTISNA = "Istisna'a"
PENDING, PARTIALLY_PAID, PAID, OVERDUE = "Pending", "Partially Paid", "Paid", "Overdue"
# `pdc_cheque` is not in the snapshot: the installment cheques own it (treasury.bank_facilities.facility_cheques)
SNAPSHOT_FIELDS = (
	"principal",
	"interest",
	"total",
	"outstanding_after",
	"paid_principal",
	"paid_interest",
	"status",
	"payment_voucher_type",
	"payment_journal",
	"repayment",
)


def _live_cheques(drawdown_name: str) -> dict:
	from treasury.bank_facilities import facility_cheques

	return facility_cheques.live_cheques(drawdown_name)


def is_cheque_repayment(repayment) -> bool:
	return repayment.get("settlement") == SETTLED_BY_CHEQUE


# ---------------------------------------------------------------------------
# Row status
# ---------------------------------------------------------------------------


def unpaid_principal(row) -> float:
	return max(flt(row.principal) - flt(row.paid_principal), 0)


def unpaid_interest(row) -> float:
	return max(flt(row.interest) - flt(row.paid_interest), 0)


def row_status(row, as_of=None) -> str:
	"""Paid / Overdue / Partially Paid / Pending of a schedule row on `as_of` (default today)."""
	precision = frappe.get_precision(SCHEDULE, "total") or 2
	if flt(flt(row.paid_principal) + flt(row.paid_interest), precision) >= flt(row.total, precision):
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
	validate_disbursed_principal(drawdown, principal)
	live = _live_cheques(drawdown.name)
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
			frappe.throw(
				_("Installment {0} does not exist on drawdown {1}.").format(
					repayment.installment_no, frappe.bold(drawdown.name)
				)
			)
		cheque = live.get(row.name)
		if cheque and not (is_cheque_repayment(repayment) and repayment.get("pdc_cheque") == cheque.name):
			frappe.throw(
				_("Installment {0} is settled by cheque {1}.").format(
					row.installment_no, frappe.bold(cheque.name)
				)
			)
		interest = take(row, "interest", interest, unpaid_interest(row))
		principal = take(row, "principal", principal, unpaid_principal(row))
	else:
		# I1: an installment with a live cheque is paid by that cheque only
		rows = [r for r in rows if r.name not in live]
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
		with_cheques = sorted(
			(r for r in drawdown.repayment_schedule if r.name in live and getdate(r.due_date) > date),
			key=lambda r: cint(r.installment_no),
		)
		if with_cheques:
			frappe.throw(
				_(
					"Installments {0} are settled by cheques: cancel or replace those cheques before a prepayment."
				).format(", ".join(str(r.installment_no) for r in with_cheques))
			)
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
	"""Untouched installments (nothing paid, no live installment cheque) due after `date`, in date order."""
	live = _live_cheques(drawdown.name)
	return sorted(
		(
			r
			for r in drawdown.repayment_schedule
			if not r.is_prepayment
			and r.name not in live
			and getdate(r.due_date) > getdate(date)
			and not flt(r.paid_principal)
			and not flt(r.paid_interest)
		),
		key=lambda r: getdate(r.due_date),
	)


def validate_disbursed_principal(drawdown, principal: float):
	"""Istisna'a: the principal repaid cannot exceed what was disbursed and is still outstanding (the ledger:
	the stages disbursed less the principal already repaid)."""
	if drawdown.limit_type != ISTISNA or flt(principal) <= 0:
		return
	from treasury.bank_facilities.utilisation import get_drawdown_outstanding

	precision = drawdown.precision("amount")
	source = {"name": drawdown.name, "liability_account": drawdown.liability_account}
	outstanding = flt(get_drawdown_outstanding([source]).get(drawdown.name), precision)
	if flt(principal, precision) > outstanding:
		frappe.throw(
			_("The principal paid {0} exceeds the disbursed principal outstanding {1}.").format(
				flt(principal, precision), outstanding
			)
		)


# ---------------------------------------------------------------------------
# Apply / undo on submit / cancel
# ---------------------------------------------------------------------------


def snapshot(drawdown) -> str:
	return json.dumps(
		[{"name": r.name, **{f: r.get(f) for f in SNAPSHOT_FIELDS}} for r in drawdown.repayment_schedule],
		default=str,
	)


def apply_repayment(drawdown, repayment, voucher: tuple[str | None, str | None]):
	"""Record the allocation of a submitted repayment on the schedule rows (and the prepayment reschedule).
	`voucher`: (doctype, name) of the repayment's voucher (Payment Entry or Journal Entry); (None, None) for a
	penalty payable to charity alone, which allocates nothing."""
	precision = drawdown.precision("amount")
	voucher_type, voucher_no = voucher
	rows = {r.name: r for r in drawdown.repayment_schedule}
	for allocation in repayment.allocations:
		row = rows.get(allocation.schedule_row)
		if not row:
			frappe.throw(_("Schedule row {0} no longer exists.").format(allocation.schedule_row))
		row.paid_principal = flt(flt(row.paid_principal) + flt(allocation.principal), precision)
		row.paid_interest = flt(flt(row.paid_interest) + flt(allocation.interest), precision)
		row.payment_voucher_type, row.payment_journal = voucher_type, voucher_no
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
				"payment_voucher_type": voucher_type,
				"payment_journal": voucher_no,
				"repayment": repayment.name,
			},
		)
	_set_outstanding_after(drawdown)
	_save_rows(drawdown)
	_bump_schedule_version(drawdown.name)
	refresh_repayment_status(drawdown.name)


def _bump_schedule_version(drawdown_name: str):
	version = cint(frappe.db.get_value(DRAWDOWN, drawdown_name, "schedule_version"))
	frappe.db.set_value(DRAWDOWN, drawdown_name, "schedule_version", version + 1, update_modified=False)


def lock_drawdown(drawdown_name: str):
	"""Lock the drawdown row (serialises its repayments and accruals) and make sure the data read so far is
	current.

	Under REPEATABLE READ the rows read before the lock (schedule, repayments, accruals) come from the
	transaction's snapshot, which may predate a repayment or accrual committed meanwhile. Every such change
	moves `schedule_version` or `last_accrual_date` of the drawdown, so comparing the snapshot values with the
	locking (current) read detects it; the user is asked to try again instead of acting on stale rows."""
	fields = ["schedule_version", "last_accrual_date"]
	seen = frappe.db.get_value(DRAWDOWN, drawdown_name, fields, as_dict=True)
	current = frappe.db.get_value(DRAWDOWN, drawdown_name, fields, as_dict=True, for_update=True)
	if seen != current:
		frappe.throw(
			_("Facility Drawdown {0} was changed by another transaction. Please try again.").format(
				frappe.bold(drawdown_name)
			),
			title=_("Concurrent Update"),
		)


def next_submission_seq(drawdown_name: str) -> int:
	"""The submission number of a repayment being submitted (call under the drawdown lock)."""
	last = frappe.get_all(
		"Facility Repayment",
		filters={"drawdown": drawdown_name, "docstatus": ["in", [1, 2]]},
		order_by="submission_seq desc",
		limit=1,
		pluck="submission_seq",
	)
	return cint(last[0] if last else 0) + 1


def check_no_later_repayment(drawdown_name: str, repayment):
	"""Only the last submitted repayment of a drawdown (highest submission number) may be cancelled; the
	cheque repayments do not count (they are undone by delta, I3)."""
	later = frappe.get_all(
		REPAYMENT,
		filters={
			"drawdown": drawdown_name,
			"docstatus": 1,
			"name": ["!=", repayment.name],
			"submission_seq": [">", cint(repayment.submission_seq)],
			"settlement": ["!=", SETTLED_BY_CHEQUE],
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


def check_can_cancel(drawdown, repayment):
	"""I3: a cheque repayment may be cancelled at any time (by its cheque); any other only when it is the last
	submitted non-cheque repayment of the drawdown."""
	if is_cheque_repayment(repayment):
		return
	check_no_later_repayment(drawdown.name, repayment)


def undo_repayment(drawdown, repayment):
	"""Undo a cancelled repayment on the schedule: a cheque repayment by subtracting its allocations (delta),
	any other by restoring its snapshot, except on the rows a live cheque or a submitted cheque repayment owns
	(those only lose this repayment's own allocations)."""
	if is_cheque_repayment(repayment):
		_undo_delta(drawdown, repayment, [a.schedule_row for a in repayment.allocations])
	else:
		check_no_later_repayment(drawdown.name, repayment)
		owned = set(_live_cheques(drawdown.name)) | _rows_paid_by_cheques(drawdown.name, repayment.name)
		saved = {r["name"]: r for r in json.loads(repayment.schedule_snapshot or "[]")}
		for row in list(drawdown.repayment_schedule):
			if row.is_prepayment and row.repayment == repayment.name:
				frappe.db.delete(SCHEDULE, {"name": row.name})
				drawdown.repayment_schedule.remove(row)
				continue
			if row.name in saved and row.name not in owned:
				values = saved[row.name]
				for fieldname in SNAPSHOT_FIELDS:
					row.set(fieldname, values.get(fieldname))
				if "payment_voucher_type" not in values and values.get("payment_journal"):
					# a snapshot taken before 1.1: the payment voucher was always a Journal Entry
					row.payment_voucher_type = "Journal Entry"
		_undo_delta(drawdown, repayment, [r for r in owned if r in saved])
	_save_rows(drawdown)
	_bump_schedule_version(drawdown.name)
	refresh_repayment_status(drawdown.name)


def _undo_delta(drawdown, repayment, row_names: list[str]):
	"""Subtract the repayment's allocations from `row_names`; their last repayment / voucher become those of the
	latest other submitted repayment allocated to them (or none)."""
	precision = drawdown.precision("amount")
	rows = {r.name: r for r in drawdown.repayment_schedule}
	wanted = set(row_names)
	for allocation in repayment.allocations:
		row = rows.get(allocation.schedule_row)
		if not row or row.name not in wanted:
			continue
		row.paid_principal = flt(max(flt(row.paid_principal) - flt(allocation.principal), 0), precision)
		row.paid_interest = flt(max(flt(row.paid_interest) - flt(allocation.interest), 0), precision)
	latest = _latest_other_repayments(list(wanted), repayment.name)
	for name in wanted:
		row = rows.get(name)
		if not row or row.repayment != repayment.name:
			continue
		other = latest.get(name)
		row.repayment = other.name if other else None
		row.payment_voucher_type = other.voucher_type if other else None
		row.payment_journal = other.voucher_no if other else None


def _latest_other_repayments(row_names: list[str], exclude: str) -> dict[str, frappe._dict]:
	"""{schedule row: {name, voucher_type, voucher_no}} of the latest submitted repayment (other than
	`exclude`) allocated to each row."""
	if not row_names:
		return {}
	allocation = frappe.qb.DocType("Facility Repayment Allocation")
	parent = frappe.qb.DocType(REPAYMENT)
	rows = (
		frappe.qb.from_(allocation)
		.join(parent)
		.on(parent.name == allocation.parent)
		.select(allocation.schedule_row, parent.name, parent.payment_entry, parent.journal_entry)
		.where(
			(allocation.parenttype == REPAYMENT)
			& (allocation.schedule_row.isin(row_names))
			& (parent.docstatus == 1)
			& (parent.name != exclude)
		)
		.orderby(parent.submission_seq)
	).run(as_dict=True)
	latest = {}
	for row in rows:
		if row.payment_entry:
			voucher = ("Payment Entry", row.payment_entry)
		elif row.journal_entry:
			voucher = ("Journal Entry", row.journal_entry)
		else:
			voucher = (None, None)
		latest[row.schedule_row] = frappe._dict(name=row.name, voucher_type=voucher[0], voucher_no=voucher[1])
	return latest


def _rows_paid_by_cheques(drawdown_name: str, exclude: str) -> set[str]:
	"""The schedule rows with an allocation of a submitted cheque repayment of the drawdown."""
	allocation = frappe.qb.DocType("Facility Repayment Allocation")
	parent = frappe.qb.DocType(REPAYMENT)
	return set(
		frappe.qb.from_(allocation)
		.join(parent)
		.on(parent.name == allocation.parent)
		.select(allocation.schedule_row)
		.where(
			(allocation.parenttype == REPAYMENT)
			& (parent.drawdown == drawdown_name)
			& (parent.docstatus == 1)
			& (parent.settlement == SETTLED_BY_CHEQUE)
			& (parent.name != exclude)
		)
		.run(pluck=True)
	)


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
