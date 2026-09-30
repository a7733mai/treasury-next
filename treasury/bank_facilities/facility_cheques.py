"""Installment cheques to the lender (Facility Drawdown repayment mode "PDC to Lender", spec 7g §3.4).

One outgoing post-dated cheque per installment, made out to the lender (a Supplier) from the drawdown's bank
account, created by a Cheque Batch whose rows are the unpaid installments (`make_installment_batch`). Each cheque
carries its installment (`facility_drawdown`, `facility_schedule_row`), and the schedule row carries its cheque
(`pdc_cheque`).

No double posting: the cheque's party posting *is* the repayment. It happens on the cheque date whatever the
settings say (treasury.pdc.lifecycle.FACILITY_PARTY_MODE); instead of the usual party Payment Entry, the
lifecycle calls `settle_by_cheque`, which submits a Facility Repayment (settlement "PDC Cheque") whose voucher is
one Payment Entry from the PDC Payable account to the liability / interest accounts, sourced to the cheque
(event `pdc_party_posting`, the cheque's cycle): Dr Loans, Dr Interest / Cr PDC Payable. The cheque's clearance
then moves PDC Payable to the bank as for any cheque, so the liability is touched by one voucher only.

A bounce, return or replacement undoes the cheque's postings (lifecycle `_undo_postings`, which calls
`on_postings_undone` first): the repayment is cancelled (delta undo, treasury.bank_facilities.repayment) and the
installment is unpaid again. A cancelled or replaced cheque releases its installment (`on_cheque_released`); a
replacement is linked to the same installment (`copy_to_replacement`, `link_cheque`).

A cheque is "live" (Created / Delivered): its installment is paid by it only (repayment I1).
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, fmt_money, formatdate, getdate, today

from treasury.bank_facilities import accounting, lenders, repayment

DRAWDOWN = "Facility Drawdown"
SCHEDULE = "Facility Repayment Schedule"
REPAYMENT = "Facility Repayment"
CHEQUE = "PDC Cheque"
BATCH = "Cheque Batch"
PDC_TO_LENDER = "PDC to Lender"
LIVE_STATUSES = ("Created", "Delivered")
ROLES = ("Treasury Manager", "Treasury User")
ADVANCE = "Advance (no allocation)"


def live_cheques(drawdown: str) -> dict[str, frappe._dict]:
	"""{schedule row name: {name, operational_status}} of the drawdown's rows whose installment cheque is live."""
	if not drawdown:
		return {}
	row = frappe.qb.DocType(SCHEDULE)
	cheque = frappe.qb.DocType(CHEQUE)
	rows = (
		frappe.qb.from_(row)
		.join(cheque)
		.on(cheque.name == row.pdc_cheque)
		.select(row.name.as_("row"), cheque.name, cheque.operational_status)
		.where(
			(row.parenttype == DRAWDOWN)
			& (row.parent == drawdown)
			& (cheque.operational_status.isin(LIVE_STATUSES))
		)
	).run(as_dict=True)
	return {r.row: frappe._dict(name=r.name, operational_status=r.operational_status) for r in rows}


def installment_info(drawdown) -> dict:
	"""What the drawdown form shows per installment (design 7g P11 / P14): the company's Auto Debit Posting, the
	installment cheques of the schedule rows ({row name: {name, cheque_no, operational_status}}, any status) and,
	in PDC to Lender mode, the default cheque book with its next free leaf."""
	from treasury.bank_facilities import auto_debit

	names = sorted({r.pdc_cheque for r in drawdown.repayment_schedule if r.pdc_cheque})
	cheques = {
		c.name: c
		for c in frappe.get_all(
			CHEQUE,
			filters={"name": ["in", names or [""]]},
			fields=["name", "cheque_no", "operational_status"],
		)
	}
	info = {
		"auto_debit_posting": auto_debit.posting_mode(drawdown.company) if drawdown.company else None,
		"live_statuses": list(LIVE_STATUSES),
		"cheques": {r.name: cheques[r.pdc_cheque] for r in drawdown.repayment_schedule if r.pdc_cheque in cheques},
		"has_cheques": bool(
			names or (drawdown.name and frappe.db.exists(CHEQUE, {"facility_drawdown": drawdown.name}))
		),
		"cheque_book": None,
	}
	if drawdown.docstatus == 1 and drawdown.repayment_mode == PDC_TO_LENDER:
		from treasury.pdc import batch
		from treasury.utils import source_documents

		count = len(_open_rows(drawdown))
		book = count and source_documents.default_cheque_book(drawdown.company, drawdown.bank_account, count)
		if book:
			leaves = batch.preview_leaves(book, 1)
			info["cheque_book"] = {"name": book, "next_no": leaves[0] if leaves else None}
	return info


def unpaid_total(row) -> float:
	return flt(repayment.unpaid_principal(row) + repayment.unpaid_interest(row), 2)


def _open_rows(drawdown) -> list:
	"""Unpaid installments (no prepayment row) without a live cheque, in due date order."""
	live = live_cheques(drawdown.name)
	return sorted(
		(
			r
			for r in drawdown.repayment_schedule
			if not r.is_prepayment and r.name not in live and unpaid_total(r) > 0
		),
		key=lambda r: (getdate(r.due_date), cint(r.installment_no)),
	)


def make_installment_batch(drawdown: str) -> dict:
	"""An unsaved Cheque Batch (as a dict, the 7c mapper pattern): one cheque per open installment, to the
	lender, dated its due date, for its unpaid principal + interest. May create the bank's Supplier (the lender
	of a bank facility), hence called by POST."""
	doc = frappe.get_doc(DRAWDOWN, drawdown)
	doc.check_permission("read")
	frappe.has_permission(BATCH, "create", throw=True)
	if not set(ROLES) & set(frappe.get_roles()):
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in ROLES)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)
	if doc.docstatus != 1 or doc.repayment_mode != PDC_TO_LENDER:
		frappe.throw(
			_("Drawdown {0} is not repaid by cheques to the lender (Repayment Mode {1}).").format(
				frappe.bold(doc.name), _(doc.repayment_mode or "", context=DRAWDOWN)
			)
		)
	rows = _open_rows(doc)
	if not rows:
		frappe.throw(_("Drawdown {0} has no installment left without a cheque.").format(frappe.bold(doc.name)))
	from treasury.utils import source_documents

	facility = frappe.get_doc("Bank Facility", doc.facility)
	batch = frappe.new_doc(BATCH)
	batch.direction = "Outgoing"
	batch.company = doc.company
	batch.party_type = "Supplier"
	batch.party = lenders.get_lender_supplier(facility)
	batch.party_name = frappe.db.get_value("Supplier", batch.party, "supplier_name")
	batch.purpose = "Installments"
	batch.bank_account = doc.bank_account
	batch.cheque_book = source_documents.default_cheque_book(doc.company, doc.bank_account, len(rows))
	# cheques entered for installments already due (back-entry) are issued on the drawdown date: a cheque
	# cannot be due before its issue date
	issue = getdate(today())
	if getdate(rows[0].due_date) < issue:
		issue = getdate(doc.drawdown_date)
	batch.issue_date = batch.handover_date = issue
	batch.allocation_method = ADVANCE
	batch.facility_drawdown = doc.name
	batch.remarks = _("Installment cheques of drawdown {0}").format(doc.name)
	for row in rows:
		batch.append(
			"cheques",
			{
				"due_date": row.due_date,
				"amount": unpaid_total(row),
				"facility_schedule_row": row.name,
				"installment_no": row.installment_no,
				"remarks": _("Installment {0} of drawdown {1}").format(row.installment_no, doc.name),
			},
		)
	batch.total_amount = flt(sum(flt(r.amount) for r in batch.cheques), 2)
	batch.cheque_count = len(batch.cheques)
	return batch.as_dict()


def validate_batch(batch):
	"""The Cheque Batch of installment cheques (treasury.pdc.batch.validate_batch; again under the drawdown
	lock on submit): the drawdown's lender, bank and open installments, each cheque exactly its installment.
	Its messages show the installments: the user must be able to read the drawdown."""
	if getattr(batch, "_action", None) == "submit":
		# a cheque linked by another batch moves `schedule_version` (`link_cheque`): a batch submitted
		# meanwhile is detected here instead of issuing a second cheque for the same installment
		repayment.lock_drawdown(batch.facility_drawdown)
	drawdown = frappe.get_doc(DRAWDOWN, batch.facility_drawdown)
	if not batch.flags.ignore_permissions:
		drawdown.check_permission("read")
	if drawdown.docstatus != 1 or drawdown.repayment_mode != PDC_TO_LENDER or drawdown.company != batch.company:
		frappe.throw(
			_("Drawdown {0} is not repaid by cheques to the lender (Repayment Mode {1}).").format(
				frappe.bold(drawdown.name), _(drawdown.repayment_mode or "", context=DRAWDOWN)
			)
		)
	lender = frappe.db.get_value("Bank Facility", drawdown.facility, "lender")
	if batch.party_type != "Supplier" or not lender or batch.party != lender:
		frappe.throw(
			_("The installment cheques must be made out to the lender {0}.").format(frappe.bold(lender or "-"))
		)
	if batch.direction != "Outgoing" or batch.bank_account != drawdown.bank_account:
		frappe.throw(
			_("The installment cheques must be outgoing cheques from Bank Account {0}.").format(
				frappe.bold(drawdown.bank_account)
			)
		)
	if cint(batch.get("gen_count")) or batch.get("gen_first_due_date"):
		frappe.throw(
			_(
				"Generate Cheques is not available for installment cheques; use Generate Installment Cheques on the drawdown."
			)
		)
	batch.allocation_method = ADVANCE
	rows = {r.name: r for r in drawdown.repayment_schedule if not r.is_prepayment}
	live = live_cheques(drawdown.name)
	seen = set()
	for item in batch.cheques:
		if item.reference_doctype or item.reference_name or flt(item.allocated_amount):
			frappe.throw(_("An installment cheque to the lender cannot allocate invoices."))
		row = rows.get(item.facility_schedule_row)
		if not row:
			frappe.throw(
				_("Row #{0}: the installment is not an installment of drawdown {1}.").format(
					item.idx, frappe.bold(drawdown.name)
				)
			)
		# (validated while draft and on submit: the batch's own cheques do not exist yet)
		cheque = live.get(row.name)
		if row.name in seen or cheque:
			frappe.throw(
				_("Row #{0}: installment {1} already has cheque {2}.").format(
					item.idx, row.installment_no, frappe.bold(cheque.name if cheque else "-")
				)
			)
		seen.add(row.name)
		item.installment_no = row.installment_no
		expected = unpaid_total(row)
		if flt(item.amount, 2) != expected or getdate(item.due_date) != getdate(row.due_date):
			frappe.throw(
				_("Row #{0}: the cheque must be {1} dated {2} (installment {3}).").format(
					item.idx,
					fmt_money(expected, currency=drawdown.currency),
					formatdate(row.due_date),
					row.installment_no,
				)
			)


def link_cheque(cheque):
	"""The installment's schedule row points at its (new) cheque. The drawdown's `schedule_version` moves, so
	a concurrent batch, repayment or cheque action on the drawdown sees the change (`repayment.lock_drawdown`)."""
	if cheque.get("facility_schedule_row"):
		frappe.db.set_value(SCHEDULE, cheque.facility_schedule_row, "pdc_cheque", cheque.name, update_modified=False)
		_bump_version(cheque)


def _bump_version(cheque):
	drawdown = cheque.get("facility_drawdown") or frappe.db.get_value(
		SCHEDULE, cheque.facility_schedule_row, "parent"
	)
	if drawdown:
		repayment._bump_schedule_version(drawdown)


def check_can_represent(cheque):
	"""A bounced installment cheque is presented again only while its installment is still its own and unpaid
	for its amount (after the bounce, the installment may have been paid by hand or given another cheque)."""
	row = cheque.get("facility_schedule_row") and frappe.db.get_value(
		SCHEDULE,
		cheque.facility_schedule_row,
		["installment_no", "pdc_cheque", "principal", "interest", "paid_principal", "paid_interest"],
		as_dict=True,
	)
	if not row or row.pdc_cheque != cheque.name or unpaid_total(row) != flt(cheque.amount, 2):
		frappe.throw(
			_(
				"Cheque {0} can no longer pay installment {1} of drawdown {2} (paid or changed since the bounce): replace or cancel it instead."
			).format(
				frappe.bold(cheque.name),
				row.installment_no if row else "-",
				frappe.bold(cheque.facility_drawdown),
			),
			title=_("Not Allowed"),
		)


def _cheque_chain(cheque_name: str) -> set[str]:
	"""The cheque and the cheques it replaced (backwards through `replaces`)."""
	names, current = set(), cheque_name
	while current and current not in names:
		names.add(current)
		current = frappe.db.get_value(CHEQUE, current, "replaces")
	return names


def settle_by_cheque(cheque, posting_date) -> str:
	"""The party posting of an installment cheque: the Facility Repayment of its installment and its one
	Payment Entry (see the module docstring). Runs in the cheque action's transaction; the cheque action checked
	the user (F10: the repayment is submitted with `ignore_permissions`). Returns the Payment Entry."""
	cycle = cint(cheque.cycle) or 1
	existing = frappe.get_all(
		REPAYMENT,
		filters={"pdc_cheque": cheque.name, "cheque_cycle": cycle, "docstatus": 1},
		fields=["name", "payment_entry"],
		limit=1,
	)
	if existing:
		cheque.facility_repayment = existing[0].name
		return existing[0].payment_entry
	repayment.lock_drawdown(cheque.facility_drawdown)
	drawdown = frappe.get_doc(DRAWDOWN, cheque.facility_drawdown)
	row = next((r for r in drawdown.repayment_schedule if r.name == cheque.facility_schedule_row), None)
	if not row or unpaid_total(row) <= 0:
		frappe.throw(
			_("Cheque {0} is no longer the cheque of installment {1}.").format(
				frappe.bold(cheque.name), row.installment_no if row else "-"
			)
		)
	if row.pdc_cheque != cheque.name:
		if row.pdc_cheque and row.pdc_cheque in _cheque_chain(cheque.name):
			link_cheque(cheque)  # the replacement of the linked cheque
			row.pdc_cheque = cheque.name
		else:
			frappe.throw(
				_("Cheque {0} is no longer the cheque of installment {1}.").format(
					frappe.bold(cheque.name), row.installment_no
				)
			)
	expected = unpaid_total(row)
	if flt(cheque.amount, 2) != expected:
		frappe.throw(
			_("Cheque {0} ({1}) no longer matches the unpaid installment {2} ({3}).").format(
				frappe.bold(cheque.name),
				fmt_money(cheque.amount, currency=cheque.currency),
				row.installment_no,
				fmt_money(expected, currency=drawdown.currency),
			)
		)
	doc = frappe.get_doc(
		{
			"doctype": REPAYMENT,
			"drawdown": drawdown.name,
			"repayment_date": getdate(posting_date),
			"bank_account": cheque.bank_account,
			"settlement": accounting.SETTLED_BY_CHEQUE,
			"pdc_cheque": cheque.name,
			"cheque_cycle": cycle,
			"allocation_mode": repayment.SELECTED_ROW,
			"installment_no": row.installment_no,
			"principal_paid": repayment.unpaid_principal(row),
			"interest_paid": repayment.unpaid_interest(row),
			"penalty": 0,
			"remarks": _("Installment cheque {0} ({1})").format(cheque.cheque_no, cheque.name),
		}
	)
	doc.flags.treasury_facility_service = True
	doc.flags.ignore_permissions = True  # F10: the cheque action checked the user
	doc.insert()
	doc.submit()
	cheque.facility_repayment = doc.name
	return frappe.db.get_value(REPAYMENT, doc.name, "payment_entry")


def on_postings_undone(cheque, posting_date=None, remarks=None):
	"""A bounce, return or replacement undoes the cheque's party posting: the repayment of its current cycle
	is cancelled (delta undo), so the installment is unpaid again (Overdue / Pending by its date)."""
	cycle = cint(cheque.cycle) or 1
	for name in frappe.get_all(
		REPAYMENT, filters={"pdc_cheque": cheque.name, "cheque_cycle": cycle, "docstatus": 1}, pluck="name"
	):
		doc = frappe.get_doc(REPAYMENT, name)
		doc.flags.treasury_facility_service = True
		doc.flags.ignore_permissions = True  # the cheque action checked the user
		doc.cancel()
	cheque.facility_repayment = None


def on_cheque_released(cheque):
	"""A cancelled or replaced cheque no longer holds its installment."""
	row = cheque.get("facility_schedule_row")
	if row and frappe.db.get_value(SCHEDULE, row, "pdc_cheque") == cheque.name:
		frappe.db.set_value(SCHEDULE, row, "pdc_cheque", None, update_modified=False)
		_bump_version(cheque)


def copy_to_replacement(old, new):
	"""The replacement pays the same installment (linked after its insert, `link_cheque`)."""
	new.facility_drawdown = old.facility_drawdown
	new.facility_schedule_row = old.facility_schedule_row
