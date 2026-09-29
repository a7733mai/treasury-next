"""Cheque Batch service (PLAN.md §6): one voucher for many post-dated cheques to / from one party.

A batch never posts anything itself. On submit it creates one PDC Cheque per row and, when a handover /
receipt date is set, delivers / receives each of them through `treasury.pdc.lifecycle`, which posts according
to Treasury Company Settings exactly as for a cheque entered on its own. Every cheque keeps its own Payment
Entry / Journal Entries and its own lifecycle; the batch only links them (`cheque_batch`, `cheque_batch_row`).

Why not a multi-cheque Payment Entry: a Payment Entry carries one amount, one reference no / date and one
clearance date, and the Bank Reconciliation matches each Payment Entry as a whole. Twelve cheques clearing on
twelve dates need twelve vouchers, which is what the PDC Cheques give without touching ERPNext.

Atomicity: submit and cancel run inside a savepoint taken in `before_submit` / `before_cancel` (before Frappe
writes the new docstatus); any failing row rolls back the batch, every cheque, voucher and cheque book leaf,
and the error names the row. The bulk actions (Deliver / Receive All) instead isolate each cheque in its own
savepoint and report successes and failures.

Header and row validation reuse the PDC Cheque validators on a transient cheque that is never inserted
(`transient_cheque`), so the batch accepts exactly what a PDC Cheque accepts.

Size and locking: submit (with a handover date) and Deliver / Receive All post every cheque in one web request
and one transaction, holding the locks of the batch, the cheque book and the bank account until the commit.
`MAX_ROWS` keeps that within the request timeout; during such a batch-wide operation the per-cheque refresh of
the batch status is skipped (`batch_operation`) and the status is refreshed once at the end.

Once the batch is submitted, the identity and amount of its cheques are frozen outside the cheque actions
(`PDCCheque.validate_locked_fields`), and the batch receipt prints the live cheques (replacements included,
`get_receipt_cheques`): the voucher the party signs always matches the real cheques.
"""

import re
from contextlib import contextmanager

import frappe
from frappe import _
from frappe.query_builder.functions import IfNull, Sum
from frappe.utils import add_days, cint, flt, getdate

import erpnext
from erpnext.accounts.doctype.account.account import get_account_currency
from treasury.accounting import posting
from treasury.pdc import lifecycle
from treasury.pdc.batch_status import (
	RELEASED_STATUSES,
	compute_batch_status,
	lock_batch,
	update_batch_status,
)
from treasury.pdc.doctype.cheque_book.cheque_book import SCAN_CHUNK
from treasury.utils.dates import monthly_dates

CHEQUE = "PDC Cheque"
BATCH = "Cheque Batch"
ITEM = "Cheque Batch Item"
# Name given to the transient cheques used for validation (never inserted); keeps the duplicate cheque number
# check of PDC Cheque from comparing against an empty name.
TRANSIENT_NAME = "__cheque_batch_validation__"
# Every cheque of a batch may be inserted, handed over and posted (a Payment Entry each) in one request:
# 120 (ten years of monthly cheques) stays well within the web request timeout.
MAX_ROWS = 120
SUBMIT_SAVEPOINT = "treasury_cheque_batch"
ACTION_SAVEPOINT = "treasury_cheque_batch_action"

MONTHLY_FREQUENCIES = {"Monthly": 1, "Every 2 Months": 2, "Quarterly": 3, "Semi-Annually": 6, "Yearly": 12}
WEEKLY = "Weekly"
CUSTOM_DAYS = "Custom Days"
PER_CHEQUE = "Per Cheque"
SPLIT_TOTAL = "Split Total"
MANUAL = "Manual"
FIFO = "FIFO Invoices"
ADVANCE = "Advance (no allocation)"
# Invoice doctype, its party field and the party type FIFO allocation works with, per direction.
FIFO_INVOICES = {
	lifecycle.OUTGOING: ("Purchase Invoice", "supplier", "Supplier"),
	lifecycle.INCOMING: ("Sales Invoice", "customer", "Customer"),
}
# Not posted cheques in these statuses still intend to settle their references (reserved allocations).
OPEN_STATUSES = ("Created", "Delivered", "Received", "Deposited")
# Header fields copied to every cheque of the batch.
HEADER_FIELDS = ("direction", "company", "party_type", "party", "bank_account", "issue_date")


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


def schedule_dates(first_due_date, count: int, frequency: str, interval_days=0, month_end=False) -> list:
	"""`count` due dates from `first_due_date`.

	Monthly frequencies are counted from the first date (31 Jan, 28 Feb, 31 Mar: the day never drifts). With
	`month_end`, a first date on the last day of its month keeps every date on the last day of its month
	(30 Apr, 31 May, 30 Jun instead of 30 Apr, 30 May, 30 Jun).
	"""
	first = getdate(first_due_date)
	if frequency in MONTHLY_FREQUENCIES:
		return monthly_dates(first, count, MONTHLY_FREQUENCIES[frequency], keep_month_end=month_end)
	if frequency == WEEKLY:
		days = 7
	elif frequency == CUSTOM_DAYS:
		days = cint(interval_days)
		if days <= 0:
			frappe.throw(_("Interval (Days) must be greater than zero."))
	else:
		frappe.throw(_("Invalid Frequency: {0}").format(frequency))
	return [getdate(add_days(first, i * days)) for i in range(count)]


def split_amounts(mode: str, count: int, amount, total, precision: int) -> list[float]:
	"""Row amounts: the same amount per cheque, or a total split evenly.

	Split Total works in the smallest unit of the precision: every cheque gets the total divided by the count
	rounded down, and the remainder (fewer units than cheques) goes one unit each to the last cheques, so the
	sum is exact and no two cheques differ by more than one unit (100,000 / 12 = 8 x 8,333.33 + 4 x 8,333.34).
	"""
	if mode == PER_CHEQUE:
		value = flt(amount, precision)
		if value <= 0:
			frappe.throw(_("Amount per Cheque must be greater than zero."))
		return [value] * count
	if mode != SPLIT_TOTAL:
		frappe.throw(_("Invalid Amount Mode: {0}").format(mode))
	total = flt(total, precision)
	if total <= 0:
		frappe.throw(_("Total Amount to Split must be greater than zero."))
	scale = 10**precision
	units = round(total * scale)
	share, remainder = divmod(units, count)
	if share <= 0:
		frappe.throw(_("The total {0} is too small to be split into {1} cheques.").format(total, count))
	return [flt((share + (1 if i >= count - remainder else 0)) / scale, precision) for i in range(count)]


def next_cheque_numbers(first: str | None, count: int) -> list[str]:
	"""`first` and the following numbers, keeping its prefix and zero padding ("000098" → "000099", "000100")."""
	first = (first or "").strip()
	if not first:
		frappe.throw(_("First Cheque No is required to number the cheques."))
	match = re.fullmatch(r"(.*?)(\d+)", first)
	if not match:
		frappe.throw(_("First Cheque No {0} must end with digits.").format(frappe.bold(first)))
	prefix, digits = match.groups()
	start = int(digits)
	return [f"{prefix}{str(start + i).zfill(len(digits))}" for i in range(count)]


def preview_leaves(cheque_book: str, count: int, reserved=()) -> list[str]:
	"""The next `count` free leaves of `cheque_book` without allocating them (they are allocated on submit).

	`reserved`: cheque numbers typed on rows of the batch, which the allocation will find used and skip.
	"""
	book = frappe.get_doc("Cheque Book", cheque_book)
	if book.status != "Active":
		return []
	skip = {int(n) for n in (str(v).strip() for v in reserved) if n.isdigit()} | book.cancelled_numbers()
	leaves = []
	number = max(cint(book.next_no), cint(book.start_no))
	end = cint(book.end_no)
	# one query per chunk of leaves (like Cheque Book.find_available_leaf), not one per leaf
	while len(leaves) < count and number <= end:
		chunk = [n for n in range(number, min(number + SCAN_CHUNK, end + 1)) if n not in skip]
		used = book.used_numbers(chunk)
		leaves += [book.format_leaf(n) for n in chunk if n not in used][: count - len(leaves)]
		number = min(number + SCAN_CHUNK, end + 1)
	return leaves


def generate_rows(batch) -> dict:
	"""Replace the rows of a draft batch by the ones described in its generator section."""
	count = cint(batch.gen_count)
	if count <= 0:
		frappe.throw(_("Number of Cheques must be greater than zero."))
	if count > MAX_ROWS:
		frappe.throw(_("A batch can hold at most {0} cheques.").format(MAX_ROWS))
	if not batch.gen_first_due_date:
		frappe.throw(_("First Due Date is required."))

	dates = schedule_dates(
		batch.gen_first_due_date, count, batch.gen_frequency, batch.gen_interval_days, batch.gen_month_end
	)
	amounts = split_amounts(
		batch.gen_amount_mode, count, batch.gen_amount, batch.gen_total, _precision(ITEM, "amount")
	)
	uses_book = _uses_cheque_book(batch)
	numbers = [None] * count if uses_book else next_cheque_numbers(batch.gen_first_cheque_no, count)
	incoming = batch.direction == lifecycle.INCOMING

	batch.set("cheques", [])
	for due_date, amount, cheque_no in zip(dates, amounts, numbers, strict=True):
		batch.append(
			"cheques",
			{
				"due_date": due_date,
				"amount": amount,
				"cheque_no": cheque_no,
				"drawer_bank": batch.gen_drawer_bank if incoming else None,
			},
		)
	set_totals(batch)
	result = {"rows": count, "total_amount": batch.total_amount}
	if uses_book:
		result["cheque_numbers"] = preview_leaves(batch.cheque_book, count)
	return result


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def transient_cheque(batch, row=None):
	"""A PDC Cheque built from the batch header (and `row`), never inserted: runs the PDC Cheque validators."""
	cheque = frappe.new_doc(CHEQUE)
	cheque.name = TRANSIENT_NAME
	# validation only: no row lock for the duplicate cheque number check (a draft save must not lock the
	# bank account; the real cheques are checked under the lock on submit)
	cheque.flags.treasury_batch_validation = True
	for fieldname in HEADER_FIELDS:
		cheque.set(fieldname, batch.get(fieldname))
	cheque.cheque_book = batch.cheque_book if batch.direction == lifecycle.OUTGOING else None
	cheque.currency = batch.currency
	if row:
		cheque.cheque_no = row.cheque_no
		cheque.amount = row.amount
		cheque.original_due_date = row.due_date
		cheque.drawer_bank = row.drawer_bank
		if row.reference_name:
			# numbered like the batch row, so the PDC Cheque messages ("Row #n: ...") point at it
			cheque.append("references", _reference(row)).idx = row.idx
	return cheque


def validate_batch(batch):
	header = transient_cheque(batch)
	header.validate_party()
	batch.party_name = header.party_name
	header.validate_bank_account()
	header.set_currency_and_rate()
	batch.currency = header.currency
	if batch.direction != lifecycle.OUTGOING:
		batch.cheque_book = None
	header.validate_cheque_book()
	batch.posting_mode = get_posting_mode(batch.company, batch.direction)
	if batch.handover_date and getdate(batch.handover_date) < getdate(batch.issue_date):
		frappe.throw(_("The Handover / Receipt Date cannot be before the Issue Date."))
	if not batch.cheques:
		frappe.throw(_("Add at least one cheque (use Generate Cheques to create the rows)."))
	if len(batch.cheques) > MAX_ROWS:
		frappe.throw(_("A batch can hold at most {0} cheques.").format(MAX_ROWS))

	validate_rows(batch)
	validate_allocations(batch)
	set_totals(batch)


def validate_rows(batch):
	precision = _precision(ITEM, "amount")
	uses_book = _uses_cheque_book(batch)
	seen = {}
	needs_leaf = 0
	for row in batch.cheques:
		row.cheque_no = (row.cheque_no or "").strip()
		row.amount = flt(row.amount, precision)
		if batch.direction == lifecycle.OUTGOING:
			row.drawer_bank = None
		if row.amount <= 0:
			frappe.throw(_("Row #{0}: Amount must be greater than zero.").format(row.idx))
		if not row.due_date:
			frappe.throw(_("Row #{0}: Due Date is required.").format(row.idx))
		if row.cheque_no:
			if row.cheque_no in seen:
				frappe.throw(
					_("Row #{0}: Cheque No {1} is already used in row #{2}.").format(
						row.idx, frappe.bold(row.cheque_no), seen[row.cheque_no]
					)
				)
			seen[row.cheque_no] = row.idx
		elif uses_book:
			needs_leaf += 1

		cheque = transient_cheque(batch, row)
		for_row(row, cheque.validate_dates)
		for_row(row, cheque.validate_cheque_book)
		if row.cheque_no or not uses_book:
			for_row(row, cheque.validate_cheque_no)

	if needs_leaf:
		leaves = preview_leaves(batch.cheque_book, needs_leaf, reserved=seen)
		if len(leaves) < needs_leaf:
			frappe.throw(
				_("Cheque Book {0} has only {1} free leaves; this batch needs {2}.").format(
					frappe.bold(batch.cheque_book), len(leaves), needs_leaf
				)
			)


def validate_allocations(batch):
	"""One reference per row; a row allocates at most its amount, an invoice / order at most what is still
	outstanding on it after the allocations of other not yet posted cheques (summed over the batch rows)."""
	precision = _precision("PDC Cheque Reference", "allocated_amount")
	company_currency = erpnext.get_company_currency(batch.company)
	totals: dict[tuple[str, str], float] = {}
	for row in batch.cheques:
		has_reference = row.reference_doctype or row.reference_name or flt(row.allocated_amount)
		if not has_reference:
			row.reference_doctype = row.reference_name = None
			row.allocated_amount = 0
			continue
		if batch.allocation_method == ADVANCE:
			frappe.throw(
				_("Row #{0}: the Allocation Method is {1}; remove the reference.").format(row.idx, _(ADVANCE))
			)
		if not (row.reference_doctype and row.reference_name):
			frappe.throw(_("Row #{0}: Reference Type and Reference Name are both required.").format(row.idx))
		row.allocated_amount = flt(row.allocated_amount, precision)
		# allocations are in the party account currency: comparable to the cheque in the company currency only
		if batch.currency == company_currency and row.allocated_amount > flt(row.amount, precision):
			frappe.throw(
				_("Row #{0}: the Allocated Amount cannot be greater than the cheque amount.").format(row.idx)
			)
		transient_cheque(batch, row).validate_references()
		key = (row.reference_doctype, row.reference_name)
		totals[key] = flt(totals.get(key, 0) + row.allocated_amount, precision)

	if not totals:
		if batch.allocation_method == FIFO:
			frappe.msgprint(
				_(
					"The Allocation Method is FIFO Invoices, but no cheque is allocated: click Allocate FIFO, otherwise every cheque is posted as an advance."
				),
				title=_("Not Allocated"),
				indicator="orange",
			)
		return
	party_currency = get_account_currency(posting.get_party_account(batch.party_type, batch.party, batch.company))
	for (doctype, name), allocated in totals.items():
		outstanding = posting.get_reference_outstanding(
			doctype, name, batch.party_type, batch.party, party_currency, precision
		)
		reserved = get_reserved_amounts(doctype, [name], exclude_batch=batch.name).get(name, 0)
		available = max(flt(outstanding - reserved, precision), 0)
		if allocated > available:
			frappe.throw(
				_(
					"{0} {1}: {2} is allocated in this batch, but only {3} is still outstanding (other not posted cheques already allocate {4})."
				).format(_(doctype), frappe.bold(name), allocated, available, reserved),
				title=_("Over Allocation"),
			)


def set_totals(batch):
	precision = _precision(BATCH, "total_amount")
	batch.total_amount = flt(sum(flt(row.amount) for row in batch.cheques), precision)
	batch.cheque_count = len(batch.cheques)


def get_posting_mode(company: str | None, direction: str | None) -> str | None:
	"""Party posting mode of the direction in the company's Treasury settings (shown on the batch)."""
	if not company or direction not in (lifecycle.OUTGOING, lifecycle.INCOMING):
		return None
	fieldname = "outgoing_party_posting" if direction == lifecycle.OUTGOING else "incoming_party_posting"
	return posting.get_company_settings(company).get(fieldname)


def for_row(row, fn):
	"""Run `fn`; a validation / permission error is raised again prefixed with the batch row number.

	Any other error (duplicate name, deadlock, lock timeout, ...) keeps its type, so Frappe still reports it
	as what it is, and the row number is added to the messages shown to the user and to the traceback.
	"""
	try:
		return fn()
	except (frappe.ValidationError, frappe.PermissionError) as e:
		_drop_error_message(e)
		frappe.throw(_("Row #{0}: {1}").format(row.idx, _message(e)), exc=type(e))
	except Exception as e:
		e.add_note(f"Cheque Batch row #{row.idx}")
		frappe.msgprint(
			_("Row #{0}: {1}").format(row.idx, _message(e)),
			title=_("Error", context="Cheque Batch"),
			indicator="red",
		)
		raise


# ---------------------------------------------------------------------------
# FIFO allocation
# ---------------------------------------------------------------------------


def allocate_fifo(batch) -> dict:
	"""Allocate the party's outstanding invoices to the rows, oldest invoice to earliest due cheque.

	One reference per row: a row takes what is left of the current invoice (up to its amount); the rest of a
	row whose invoice is used up stays unallocated (an advance), and so do the rows after the last invoice.
	So an advance can remain while later invoices are still open (e.g. invoices 1,500 + 1,500 and three
	cheques of 1,000 leave 500 as an advance and 500 of the second invoice unpaid): the summary reports that
	as `not_covered`, to be allocated by hand (or settled later by ERPNext's Payment Reconciliation).
	"""
	doctype, _party_field, party_type = FIFO_INVOICES[batch.direction]
	if batch.party_type != party_type or not batch.party:
		frappe.throw(
			_("FIFO allocation of {0} cheques needs a {1} as the party.").format(
				_(batch.direction), _(party_type)
			)
		)
	if not batch.cheques:
		frappe.throw(_("Generate or add the cheques first."))
	_require_party_currency(batch)

	precision = _precision("PDC Cheque Reference", "allocated_amount")
	invoices = get_outstanding_invoices(batch)
	queue = [[d.name, d.available] for d in invoices if d.available > 0]
	allocated_per_invoice: dict[str, float] = {}
	for row in batch.cheques:
		row.reference_doctype = row.reference_name = None
		row.allocated_amount = 0
	for row in sorted(batch.cheques, key=lambda r: (getdate(r.due_date), r.idx)):
		if not queue:
			break
		name, available = queue[0]
		take = flt(min(flt(row.amount, precision), available), precision)
		if take <= 0:
			continue
		row.reference_doctype, row.reference_name, row.allocated_amount = doctype, name, take
		allocated_per_invoice[name] = flt(allocated_per_invoice.get(name, 0) + take, precision)
		queue[0][1] = flt(available - take, precision)
		if queue[0][1] <= 0:
			queue.pop(0)

	batch.allocation_method = FIFO
	set_totals(batch)
	allocated = flt(sum(allocated_per_invoice.values()), precision)
	return {
		"invoices": [
			{
				"reference_doctype": doctype,
				"reference_name": d.name,
				"outstanding": d.available,
				"allocated": allocated_per_invoice.get(d.name, 0),
			}
			for d in invoices
		],
		"allocated": allocated,
		"unallocated": flt(batch.total_amount - allocated, precision),
		# outstanding of the invoices that the cheques do not settle (see the docstring)
		"not_covered": flt(sum(d.available for d in invoices) - allocated, precision),
	}


def get_outstanding_invoices(batch) -> list[frappe._dict]:
	"""The party's submitted invoices of the company with an outstanding amount, oldest due first; `available`
	is the outstanding less what other not yet posted cheques already allocate to them."""
	doctype, party_field, _party_type = FIFO_INVOICES[batch.direction]
	precision = _precision("PDC Cheque Reference", "allocated_amount")
	invoices = frappe.get_all(
		doctype,
		filters={
			"company": batch.company,
			party_field: batch.party,
			"docstatus": 1,
			"outstanding_amount": [">", 0],
		},
		fields=["name", "outstanding_amount", "due_date", "posting_date"],
		order_by="due_date asc, posting_date asc, name asc",
	)
	reserved = get_reserved_amounts(doctype, [d.name for d in invoices], exclude_batch=batch.name)
	for d in invoices:
		d.available = max(flt(flt(d.outstanding_amount) - reserved.get(d.name, 0), precision), 0)
	return invoices


def get_reserved_amounts(reference_doctype: str, names: list[str], exclude_batch: str | None = None) -> dict:
	"""{reference name: amount} allocated by open PDC Cheques not posted to the party yet (other batches and
	single cheques; the cheques of `exclude_batch` are left out)."""
	if not names:
		return {}
	ref = frappe.qb.DocType("PDC Cheque Reference")
	cheque = frappe.qb.DocType(CHEQUE)
	query = (
		frappe.qb.from_(ref)
		.join(cheque)
		.on(ref.parent == cheque.name)
		.select(ref.reference_name, Sum(ref.allocated_amount))
		.where(
			(ref.parenttype == CHEQUE)
			& (ref.reference_doctype == reference_doctype)
			& (ref.reference_name.isin(names))
			& (cheque.accounting_status == lifecycle.NOT_POSTED)
			& (cheque.operational_status.isin(OPEN_STATUSES))
		)
		.groupby(ref.reference_name)
	)
	if exclude_batch:
		query = query.where(IfNull(cheque.cheque_batch, "") != exclude_batch)
	return {name: flt(amount) for name, amount in query.run()}


def _require_party_currency(batch):
	"""FIFO compares invoice outstanding (party account currency) with cheque amounts (bank currency)."""
	header = transient_cheque(batch)
	header.set_currency_and_rate()  # the batch may not be saved yet
	batch.currency = header.currency
	party_account = posting.get_party_account(batch.party_type, batch.party, batch.company)
	party_currency = get_account_currency(party_account)
	if party_currency != batch.currency:
		frappe.throw(
			_(
				"FIFO allocation needs the party account in the cheque currency {0} (it is in {1}). Allocate the rows manually."
			).format(batch.currency, party_currency)
		)


# ---------------------------------------------------------------------------
# Submit / cancel
# ---------------------------------------------------------------------------


def check_can_create_cheques():
	if not frappe.has_permission(CHEQUE, "create"):
		frappe.throw(
			_("You need permission to create PDC Cheques to submit a Cheque Batch."),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def begin_atomic(batch):
	"""Savepoint taken before Frappe writes the new docstatus (called from before_submit / before_cancel)."""
	frappe.db.savepoint(SUBMIT_SAVEPOINT)
	batch.flags.treasury_savepoint = True


def run_atomic(batch, fn):
	"""Run `fn(batch)` (from on_submit / on_cancel); on any error undo everything since `begin_atomic`."""
	try:
		fn(batch)
	except Exception:
		if batch.flags.pop("treasury_savepoint", None):
			frappe.db.rollback(save_point=SUBMIT_SAVEPOINT)
		raise
	if batch.flags.pop("treasury_savepoint", None):
		frappe.db.release_savepoint(SUBMIT_SAVEPOINT)


@contextmanager
def batch_operation(batch_name: str):
	"""Act on all the cheques of a batch: lock the batch row (before any cheque, like `lifecycle._begin`)
	and skip the per-cheque refresh of the batch status meanwhile; the caller refreshes it once afterwards."""
	lock_batch(batch_name)
	previous = frappe.flags.get("treasury_batch_operation")
	frappe.flags.treasury_batch_operation = batch_name
	try:
		yield
	finally:
		frappe.flags.treasury_batch_operation = previous


def create_cheques(batch):
	"""One PDC Cheque per row, handed over / received on the batch's date if it has one. Idempotent: a row
	that already has its cheque keeps it, and only Created cheques are handed over."""
	with batch_operation(batch.name):
		for row in batch.cheques:
			for_row(row, lambda row=row: _submit_row(batch, row))
	batch.status = update_batch_status(batch.name)


def _submit_row(batch, row):
	name = row.pdc_cheque or frappe.db.get_value(
		CHEQUE, {"cheque_batch": batch.name, "cheque_batch_row": row.name}, "name"
	)
	cheque = frappe.get_doc(CHEQUE, name) if name else _insert_cheque(batch, row)
	if row.pdc_cheque != cheque.name or row.cheque_no != cheque.cheque_no:
		row.db_set({"pdc_cheque": cheque.name, "cheque_no": cheque.cheque_no}, update_modified=False)
	if batch.handover_date and cheque.operational_status == "Created":
		action = lifecycle.deliver if batch.direction == lifecycle.OUTGOING else lifecycle.receive
		action(cheque.name, batch.handover_date, _("Cheque Batch {0}").format(batch.name))


def _insert_cheque(batch, row):
	cheque = frappe.new_doc(CHEQUE)
	for fieldname in HEADER_FIELDS:
		cheque.set(fieldname, batch.get(fieldname))
	if batch.direction == lifecycle.OUTGOING:
		cheque.cheque_book = batch.cheque_book
	else:
		cheque.drawer_bank = row.drawer_bank
	cheque.cheque_no = row.cheque_no or None
	cheque.amount = row.amount
	cheque.original_due_date = row.due_date
	cheque.remarks = row.remarks or batch.remarks
	if row.reference_name:
		cheque.append("references", _reference(row))
	cheque.cheque_batch = batch.name
	cheque.cheque_batch_row = row.name
	cheque.flags.treasury_cheque_batch = True  # keeps the (insert-only) batch link fields
	cheque.insert()  # the user's own create permission (check_can_create_cheques)
	return cheque


def get_batch_cheques(batch_name: str, fields: list[str] | None = None, as_user: bool = False) -> list:
	"""The cheques of a batch in creation (= row) order; replacement cheques follow their batch rows."""
	getter = frappe.get_list if as_user else frappe.get_all
	return getter(
		CHEQUE,
		filters={"cheque_batch": batch_name},
		fields=fields or ["name", "cheque_no", "operational_status", "accounting_status"],
		order_by="creation asc, name asc",
	)


def validate_cancellation(batch):
	"""A batch is cancelled only while none of its cheques is in use (all Created and not posted, or already
	Cancelled / Replaced). Otherwise every blocking cheque is listed: the user acts on each of them.

	The batch row is locked first, so no cheque of the batch can be handed over between this check and the
	cancellation; cancelling the Created cheques needs the Treasury Manager role (`lifecycle.cancel_cheque`),
	checked here before any work is done."""
	lock_batch(batch.name)
	cheque = frappe.qb.DocType(CHEQUE)
	cheques = (
		frappe.qb.from_(cheque)
		.select(cheque.name, cheque.operational_status, cheque.accounting_status)
		.where(cheque.cheque_batch == batch.name)
		.orderby(cheque.creation)
		.orderby(cheque.name)
		.for_update()  # the latest statuses, not the transaction's snapshot
		.run(as_dict=True)
	)
	blocking = [
		c
		for c in cheques
		if c.operational_status not in RELEASED_STATUSES
		and not (c.operational_status == "Created" and c.accounting_status == lifecycle.NOT_POSTED)
	]
	if blocking:
		frappe.throw(
			_(
				"Cheque Batch {0} cannot be cancelled because these cheques are handed over or posted: {1}. Bounce, return, replace or cancel each of them first."
			).format(
				frappe.bold(batch.name),
				", ".join(
					f"{c.name} ({_(c.operational_status)}, {_(c.accounting_status)})" for c in blocking
				),
			),
			title=_("Cancellation Not Allowed"),
		)
	if any(c.operational_status == "Created" for c in cheques) and lifecycle.MANAGER not in frappe.get_roles():
		frappe.throw(
			_("Cancelling Cheque Batch {0} cancels its cheques, which requires the role {1}.").format(
				frappe.bold(batch.name), _(lifecycle.MANAGER)
			),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def cancel_cheques(batch):
	"""Cancel the batch's Created cheques through the lifecycle (records their leaves as cancelled)."""
	reason = _("Cheque Batch {0} cancelled").format(batch.name)
	with batch_operation(batch.name):
		for cheque in get_batch_cheques(batch.name):
			if cheque.operational_status == "Created":
				lifecycle.cancel_cheque(cheque.name, reason)
	batch.status = update_batch_status(batch.name)


def clear_cancelled_leaves(batch):
	"""An amended batch copies the rows of the cancelled one, whose book leaves were cancelled with it: those
	numbers are cleared so the submit allocates fresh leaves."""
	if batch.direction != lifecycle.OUTGOING or not batch.cheque_book:
		return
	cancelled = frappe.get_doc("Cheque Book", batch.cheque_book).cancelled_numbers()
	for row in batch.cheques:
		value = (row.cheque_no or "").strip()
		if value.isdigit() and int(value) in cancelled:
			row.cheque_no = None


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def run_bulk_action(batch, direction: str, date=None, remarks: str | None = None) -> dict:
	"""Deliver (outgoing) / receive (incoming) every Created cheque of a submitted batch.

	Each cheque runs in its own savepoint with the lifecycle's own permission and role checks: a failing
	cheque is rolled back and reported, the others go on. Returns {"succeeded", "failed", "skipped"}.

	Rolling back to the savepoint undoes the failed cheque's database writes and drops the messages it
	queued (e.g. ERPNext's "Allocation Adjusted"). It cannot undo `frappe.db.after_commit` callbacks or jobs
	queued with `enqueue_after_commit` by a voucher submitted before the failure: those still run at the
	commit (a Frappe limitation, see `Database.rollback`), so they must tolerate a voucher that no longer
	exists.
	"""
	if batch.docstatus != 1:
		frappe.throw(_("Submit the Cheque Batch first."))
	if batch.direction != direction:
		frappe.throw(_("This action is only available for {0} cheques.").format(_(direction)))
	action = lifecycle.deliver if direction == lifecycle.OUTGOING else lifecycle.receive
	result = {"succeeded": [], "failed": [], "skipped": []}
	with batch_operation(batch.name):
		for cheque in get_batch_cheques(batch.name):
			if cheque.operational_status != "Created":
				result["skipped"].append(cheque.name)
				continue
			messages = len(frappe.local.message_log)
			frappe.db.savepoint(ACTION_SAVEPOINT)
			try:
				action(cheque.name, date, remarks)
			except Exception as e:
				frappe.db.rollback(save_point=ACTION_SAVEPOINT)
				del frappe.local.message_log[messages:]
				result["failed"].append(
					{"cheque": cheque.name, "cheque_no": cheque.cheque_no, "error": _message(e)}
				)
			else:
				frappe.db.release_savepoint(ACTION_SAVEPOINT)
				result["succeeded"].append(cheque.name)
	update_batch_status(batch.name)
	return result


def get_print_info(batch) -> dict:
	"""The batch's cheques to print (not Cancelled / Replaced) and the cheque print format of its bank."""
	names = [
		c.name
		for c in get_batch_cheques(batch.name, as_user=True)
		if c.operational_status not in RELEASED_STATUSES
	]
	print_format = transient_cheque(batch).get_cheque_print_format() or "PDC Cheque Print"
	return {"doctype": CHEQUE, "names": names, "print_format": print_format}


def get_receipt_cheques(batch) -> dict:
	"""The cheques printed on the Cheque Batch Receipt: {"cheques", "total", "count"}.

	A submitted batch prints its live cheques, not its frozen rows: replacements included (with their own
	number, amount and due date), Cancelled / Replaced cheques left out, in batch row order. So the voucher
	the party signs lists exactly the cheques that exist. A draft or cancelled batch prints its rows.
	"""
	incoming = batch.direction == lifecycle.INCOMING
	if batch.docstatus == 1:
		row_idx = {row.name: row.idx for row in batch.cheques}
		cheques = [
			frappe._dict(
				cheque_no=c.cheque_no,
				due_date=c.effective_due_date,
				amount=c.amount,
				bank=c.drawer_bank if incoming else c.bank_account,
				pdc_cheque=c.name,
			)
			for c in sorted(
				frappe.get_all(
					CHEQUE,
					filters={"cheque_batch": batch.name, "operational_status": ["not in", RELEASED_STATUSES]},
					fields=[
						"name",
						"cheque_no",
						"effective_due_date",
						"amount",
						"drawer_bank",
						"bank_account",
						"cheque_batch_row",
					],
					order_by="creation asc, name asc",
				),
				# stable: a replacement follows the cheques of earlier rows, in creation order
				key=lambda c: row_idx.get(c.cheque_batch_row, len(row_idx) + 1),
			)
		]
	else:
		cheques = [
			frappe._dict(
				cheque_no=row.cheque_no,
				due_date=row.due_date,
				amount=row.amount,
				bank=row.drawer_bank if incoming else batch.bank_account,
				pdc_cheque=row.pdc_cheque,
			)
			for row in batch.cheques
		]
	total = flt(sum(flt(c.amount) for c in cheques), _precision(BATCH, "total_amount"))
	return {"cheques": cheques, "total": total, "count": len(cheques)}


def get_dashboard(batch) -> list:
	"""Cheques of the batch with their statuses, for the form dashboard (read with the user's permissions)."""
	return get_batch_cheques(
		batch.name,
		fields=[
			"name",
			"cheque_no",
			"amount",
			"currency",
			"effective_due_date",
			"operational_status",
			"accounting_status",
			"party_payment_entry",
			"cheque_batch_row",
		],
		as_user=True,
	)


def current_status(batch) -> str:
	statuses = [c.operational_status for c in get_batch_cheques(batch.name)] if batch.docstatus == 1 else []
	return compute_batch_status(batch.docstatus, batch.direction, statuses)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uses_cheque_book(batch) -> bool:
	return batch.direction == lifecycle.OUTGOING and bool(batch.cheque_book)


def _reference(row) -> dict:
	return {
		"reference_doctype": row.reference_doctype,
		"reference_name": row.reference_name,
		"allocated_amount": row.allocated_amount,
	}


def _precision(doctype: str, fieldname: str) -> int:
	return frappe.get_precision(doctype, fieldname) or 2


def _message(exc: Exception) -> str:
	return str(exc.args[0]) if exc.args else str(exc) or exc.__class__.__name__


def _drop_error_message(exc: Exception):
	"""Remove the dialog message of a caught `frappe.throw` (it is reported another way)."""
	exc_id = getattr(exc, "__frappe_exc_id", None)
	if exc_id:
		frappe.local.message_log = [
			m for m in frappe.local.message_log if not (isinstance(m, dict) and m.get("__frappe_exc_id") == exc_id)
		]
