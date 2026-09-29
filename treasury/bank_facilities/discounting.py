"""Cheque Discounting (خصم أوراق تجارية, PLAN.md §8): received customer cheques discounted at the bank.

Submit (Discount): the batch total is checked against the facility's Cheque Discounting sub-limit and total
limit (facility row locked). One Journal Entry: Dr Bank (net proceeds) + Dr Discount Expense (discount
charges + commission) / Cr Discounted Liability (the total, on the Liability Account of the sub-limit). Every
cheque then goes through the PDC lifecycle action `discount`: operational status Discounted, accounting status
unchanged — the cheque stays in PDC Receivable until the drawer pays.

Discount charges: per cheque, amount x rate / 100 x days from the discount date to the cheque's effective due
date / 365 (Actual/365) or 360 (Actual/360), rounded per cheque; the batch charges are their sum. The
commission is the sub-limit's Commission Rate x the total, recomputed on every save unless "Set Commission
Manually" is checked.

Per cheque afterwards (PDC lifecycle, vouchers sourced to the cheque):
- `settle_discounted` (the drawer paid the bank at maturity; from the batch's "Settle Cheques" or the cheque;
  not before the cheque's effective due date): Dr Discounted Liability / Cr PDC Receivable; the cheque is
  Cleared / Bank Posted (`bank_journal`).
- `bounce` (recourse): Dr Discounted Liability / Cr Bank (the bank debits the company), then the party
  posting is undone as for any bounce (Treasury Company Settings `on_bounce`).
The batch item records the outcome (Settled / Bounced, date, Journal Entry; written only by `close_item`,
never editable on the submitted batch) and the batch status follows: Discounted → Partially Settled →
Settled (every cheque paid or bounced).

Permissions: only a Treasury Manager submits / cancels a batch (its submit and cancel run the Treasury
Manager actions `discount` / `undiscount` on the cheques); System Manager may prepare drafts only.

Cancel: only while no cheque is settled or bounced; the cheques go back to Received (`undiscount`) and the
batch Journal Entry is cancelled.

Utilisation (hook `treasury_facility_utilisation_sources`): the discounted cheques not paid or bounced yet
(on a past date: discounted on or before it and not closed by then).
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import date_diff, flt, formatdate, getdate

from treasury.accounting import posting
from treasury.bank_facilities import accounting, instruments
from treasury.bank_facilities.utilisation import refresh_utilisation

DOCTYPE = "Cheque Discounting"
ITEM_DOCTYPE = "Cheque Discounting Item"
LIMIT_TYPE = "Cheque Discounting"
EVENT_DISCOUNTING = "cheque_discounting"

DRAFT = "Draft"
DISCOUNTED = "Discounted"
PARTIALLY_SETTLED = "Partially Settled"
SETTLED = "Settled"
CANCELLED = "Cancelled"
# Batches with cheques still discounted (not paid or bounced yet): they count against the facility.
OPEN_STATUSES = (DISCOUNTED, PARTIALLY_SETTLED)

ITEM_DISCOUNTED = "Discounted"
ITEM_SETTLED = "Settled"
ITEM_BOUNCED = "Bounced"

DAY_COUNT_BASIS = {"Actual/365": 365, "Actual/360": 360}


# ---------------------------------------------------------------------------
# Batch (controller hooks)
# ---------------------------------------------------------------------------


def row_charges(amount: float, rate: float, days: int, day_count: str, precision: int) -> float:
	return flt(flt(amount) * flt(rate) / 100 * days / DAY_COUNT_BASIS[day_count], precision)


def discount_batch(doc):
	"""on_submit: the batch Journal Entry, then every cheque Discounted."""
	from treasury.pdc import lifecycle

	journal = post_batch(doc)
	for row in doc.items:
		lifecycle.discount(row.pdc_cheque, doc.discount_date, doc.name, journal)
	doc.outstanding_amount = doc.total_amount
	instruments.log_now(
		doc,
		"Discounted",
		DISCOUNTED,
		doc.discount_date,
		("Journal Entry", journal),
		values={"journal_entry": journal, "outstanding_amount": doc.total_amount},
	)


def post_batch(doc) -> str:
	"""Dr Bank (net) + Dr Discount Expense (charges + commission) / Cr Discounted Liability (total)."""
	rate = flt(doc.exchange_rate) or 1.0
	company, currency = doc.company, doc.currency
	rows = [
		accounting._row(
			company, currency, accounting.bank_gl_account(doc.bank_account), rate, debit=doc.net_proceeds
		)
	]
	if flt(doc.total_charges):
		rows.append(
			accounting._expense_row(
				company, doc.discount_expense_account, doc.total_charges, rate, accounting._precision()
			)
		)
	rows.append(accounting._row(company, currency, doc.liability_account, rate, credit=doc.total_amount))
	return posting.create_journal_entry(
		source_doctype=DOCTYPE,
		source_name=doc.name,
		event=EVENT_DISCOUNTING,
		company=company,
		posting_date=doc.discount_date,
		accounts=rows,
		cheque_no=doc.name,
		cheque_date=doc.discount_date,
		clearance_date=doc.discount_date,
		remarks=_("Cheque Discounting {0} of Bank Facility {1}").format(doc.name, doc.facility),
		voucher_type="Bank Entry",
		balance_exchange_difference=True,
	)


def validate_cancel(doc):
	closed = [row.pdc_cheque for row in doc.items if row.status != ITEM_DISCOUNTED]
	if closed:
		frappe.throw(
			_(
				"Cheque Discounting {0} cannot be cancelled: cheques {1} are already settled or bounced."
			).format(frappe.bold(doc.name), ", ".join(frappe.bold(c) for c in closed)),
			title=_("Not Allowed"),
		)


def cancel_batch(doc):
	"""on_cancel: the cheques go back to Received, the batch Journal Entry is cancelled."""
	from treasury.pdc import lifecycle

	for row in doc.items:
		lifecycle.undiscount(row.pdc_cheque, doc.name, _("Cheque Discounting {0} cancelled").format(doc.name))
	accounting.cancel_vouchers(DOCTYPE, doc.name)
	doc.outstanding_amount = 0
	instruments.log_now(doc, "Cancelled", CANCELLED, values={"outstanding_amount": 0})


def settle_cheques(name: str, cheques: list[str] | None = None, settlement_date=None) -> list[dict]:
	"""Settle the given discounted cheques of the batch (default: every one still Discounted and due by the
	settlement date) through the PDC lifecycle."""
	from treasury.pdc import lifecycle

	doc = frappe.get_doc(DOCTYPE, name)
	doc.check_permission("write")
	if doc.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted first.").format(_(DOCTYPE), frappe.bold(name)))
	date = instruments.action_date(settlement_date)
	open_rows = {row.pdc_cheque: row for row in doc.items if row.status == ITEM_DISCOUNTED}
	if cheques:
		unknown = [c for c in cheques if c not in open_rows]
		if unknown:
			frappe.throw(
				_("Cheques {0} are not discounted and open in {1}.").format(
					", ".join(frappe.bold(c) for c in unknown), frappe.bold(name)
				)
			)
		selected = list(cheques)
	else:
		selected = [c for c, row in open_rows.items() if getdate(row.due_date) <= date]
	if not selected:
		frappe.throw(
			_("No discounted cheque of {0} is due by {1}.").format(frappe.bold(name), formatdate(date))
		)
	return [lifecycle.settle_discounted(cheque, date) for cheque in selected]


# ---------------------------------------------------------------------------
# Per cheque (called by treasury.pdc.lifecycle)
# ---------------------------------------------------------------------------


def _batch_of(cheque):
	if not cheque.cheque_discounting:
		frappe.throw(_("Cheque {0} is not linked to a Cheque Discounting.").format(frappe.bold(cheque.name)))
	return frappe.get_doc(DOCTYPE, cheque.cheque_discounting)


def post_settlement(cheque, posting_date) -> str:
	"""Dr Discounted Liability (at the batch rate) / Cr PDC Receivable (at the cheque's carrying rate)."""
	from treasury.pdc import lifecycle

	batch = _batch_of(cheque)
	mapping = posting.get_account_mapping(cheque.company, cheque.bank_account)
	return posting.create_journal_entry(
		source_doctype=lifecycle.DOCTYPE,
		source_name=cheque.name,
		event=lifecycle.EVENT_DISCOUNT_SETTLEMENT,
		company=cheque.company,
		posting_date=posting_date,
		accounts=[
			lifecycle._row(
				cheque, batch.liability_account, flt(batch.exchange_rate) or 1.0, debit=cheque.amount
			),
			lifecycle._row(
				cheque, mapping.pdc_receivable_account, lifecycle._carrying_rate(cheque), credit=cheque.amount
			),
		],
		cheque_no=cheque.cheque_no,
		cheque_date=cheque.effective_due_date,
		remarks=_("Discounted cheque {0} ({1}) paid at maturity, {2}").format(
			cheque.cheque_no, cheque.name, batch.name
		),
		cycle=lifecycle._cycle(cheque),
		balance_exchange_difference=True,
	)


def post_recourse(cheque, posting_date) -> str:
	"""The bank takes the bounced discounted cheque's amount back: Dr Discounted Liability / Cr Bank."""
	from treasury.pdc import lifecycle

	batch = _batch_of(cheque)
	bank_rate = lifecycle._bank_rate(cheque, posting_date)
	return posting.create_journal_entry(
		source_doctype=lifecycle.DOCTYPE,
		source_name=cheque.name,
		event=lifecycle.EVENT_DISCOUNT_RECOURSE,
		company=cheque.company,
		posting_date=posting_date,
		accounts=[
			lifecycle._row(
				cheque, batch.liability_account, flt(batch.exchange_rate) or 1.0, debit=cheque.amount
			),
			lifecycle._row(
				cheque, accounting.bank_gl_account(batch.bank_account), bank_rate, credit=cheque.amount
			),
		],
		cheque_no=cheque.cheque_no,
		cheque_date=cheque.effective_due_date,
		clearance_date=posting_date,
		remarks=_("Recourse of the bounced discounted cheque {0} ({1}), {2}").format(
			cheque.cheque_no, cheque.name, batch.name
		),
		voucher_type="Bank Entry",
		cycle=lifecycle._cycle(cheque),
		balance_exchange_difference=True,
	)


def close_item(batch: str, cheque: str, status: str, date, journal: str):
	"""Record the outcome of a discounted cheque on its batch row (a submitted child row: written directly)."""
	row = frappe.db.get_value(
		ITEM_DOCTYPE,
		{"parenttype": DOCTYPE, "parent": batch, "pdc_cheque": cheque, "status": ITEM_DISCOUNTED},
		"name",
		for_update=True,
	)
	if not row:
		frappe.throw(
			_("Cheque {0} is not open in Cheque Discounting {1}.").format(frappe.bold(cheque), batch)
		)
	frappe.db.set_value(
		ITEM_DOCTYPE, row, {"status": status, "closed_date": getdate(date), "closing_journal": journal}
	)


def refresh_batch(name: str):
	"""Batch status and outstanding amount from its rows (a status change is logged), then the facility's
	display utilisation."""
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.docstatus != 1:
		return
	open_rows = [row for row in doc.items if row.status == ITEM_DISCOUNTED]
	outstanding = flt(sum(flt(row.amount) for row in open_rows), doc.precision("total_amount"))
	status = (
		DISCOUNTED if len(open_rows) == len(doc.items) else (SETTLED if not open_rows else PARTIALLY_SETTLED)
	)
	if status != doc.status or outstanding != flt(doc.outstanding_amount):
		doc.outstanding_amount = outstanding
		dates = [getdate(row.closed_date) for row in doc.items if row.closed_date]
		instruments.log_now(
			doc,
			"Cheque Closed",
			status,
			max(dates) if dates else None,
			values={"outstanding_amount": outstanding},
		)
	refresh_utilisation(doc.facility)


# ---------------------------------------------------------------------------
# Utilisation
# ---------------------------------------------------------------------------


def get_discounting_utilisation(facility, as_on=None) -> list[dict]:
	"""Hook `treasury_facility_utilisation_sources` (see the module docstring)."""
	batch = frappe.qb.DocType(DOCTYPE)
	item = frappe.qb.DocType(ITEM_DOCTYPE)
	query = (
		frappe.qb.from_(item)
		.join(batch)
		.on((batch.name == item.parent) & (item.parenttype == DOCTYPE))
		.select(Sum(item.amount))
		.where((batch.docstatus == 1) & (batch.facility == facility.name))
	)
	if as_on:
		as_on = getdate(as_on)
		query = query.where(
			(batch.discount_date <= as_on) & (item.closed_date.isnull() | (item.closed_date > as_on))
		)
	else:
		query = query.where(item.status == ITEM_DISCOUNTED)
	amount = flt(query.run()[0][0])
	return [{"limit_type": LIMIT_TYPE, "amount": amount}] if amount else []


def days_to_due(discount_date, due_date) -> int:
	return date_diff(getdate(due_date), getdate(discount_date))
