"""Treasury posting service — the only place that creates accounting vouchers.

Every voucher is a standard Payment Entry or Journal Entry linked back to its Treasury source through the
read-only custom fields `treasury_source_doctype`, `treasury_source_name` and `treasury_event`.
All creators are idempotent on (source_doctype, source_name, event).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate

import erpnext
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.party import get_party_account
from erpnext.setup.utils import get_exchange_rate

VOUCHER_DOCTYPES = ("Payment Entry", "Journal Entry")
INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")


# ---------------------------------------------------------------------------
# Settings lookups
# ---------------------------------------------------------------------------


def get_company_settings(company: str) -> Document:
	"""Return the Treasury Company Settings of `company`."""
	if not company or not frappe.db.exists("Treasury Company Settings", company):
		frappe.throw(
			_("Treasury Company Settings are missing for company {0}.").format(frappe.bold(company)),
			title=_("Missing Settings"),
		)
	return frappe.get_doc("Treasury Company Settings", company)


def get_account_mapping(company: str, bank_account: str) -> frappe._dict:
	"""Return the PDC Account Mapping row of `bank_account` in the settings of `company`."""
	settings = get_company_settings(company)
	for row in settings.pdc_accounts:
		if row.bank_account == bank_account:
			return frappe._dict(row.as_dict())

	frappe.throw(
		_("No PDC account mapping found for Bank Account {0} in Treasury Company Settings {1}.").format(
			frappe.bold(bank_account), frappe.bold(company)
		),
		title=_("Missing Settings"),
	)


# ---------------------------------------------------------------------------
# Linked vouchers
# ---------------------------------------------------------------------------


def get_linked_vouchers(source_doctype: str, source_name: str) -> list[dict]:
	"""All non-cancelled and cancelled vouchers created for a Treasury source, oldest first."""
	vouchers = []
	for doctype in VOUCHER_DOCTYPES:
		rows = frappe.get_all(
			doctype,
			filters={"treasury_source_doctype": source_doctype, "treasury_source_name": source_name},
			fields=["name", "docstatus", "posting_date", "treasury_event", "creation"],
			order_by="creation asc",
		)
		for row in rows:
			row["doctype"] = doctype
			vouchers.append(row)
	vouchers.sort(key=lambda d: d["creation"])
	return vouchers


def find_voucher(doctype: str, source_doctype: str, source_name: str, event: str) -> str | None:
	"""Return the draft/submitted voucher already created for (source, event), if any."""
	names = frappe.get_all(
		doctype,
		filters={
			"treasury_source_doctype": source_doctype,
			"treasury_source_name": source_name,
			"treasury_event": event,
			"docstatus": ["<", 2],
		},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)
	return names[0] if names else None


def _return_existing(doctype: str, name: str, submit: bool) -> str:
	doc = frappe.get_doc(doctype, name)
	if submit and doc.docstatus == 0:
		doc.flags.ignore_permissions = True
		doc.submit()
	return name


def _set_treasury_fields(doc: Document, source_doctype: str, source_name: str, event: str):
	if not frappe.db.exists(source_doctype, source_name):
		frappe.throw(_("{0} {1} does not exist").format(_(source_doctype), source_name))
	doc.treasury_source_doctype = source_doctype
	doc.treasury_source_name = source_name
	doc.treasury_event = event


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------


def create_payment_entry(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	payment_type: str,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	amount: float,
	bank_account_head: str,
	reference_no: str,
	reference_date,
	currency: str | None = None,
	exchange_rate: float | None = None,
	references: list[dict] | None = None,
	remarks: str | None = None,
	submit: bool = True,
) -> str:
	"""Create (or return the existing) Payment Entry whose bank side is `bank_account_head`.

	`amount` is expressed in the currency of `bank_account_head` (the cheque currency) and
	`exchange_rate` converts that currency to the company currency.
	"""
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event):
		return _return_existing("Payment Entry", existing, submit)

	if payment_type not in ("Pay", "Receive"):
		frappe.throw(_("Payment Type must be Pay or Receive."))

	amounts = _get_payment_amounts(
		company=company,
		posting_date=posting_date,
		party_type=party_type,
		party=party,
		amount=amount,
		bank_account_head=bank_account_head,
		currency=currency,
		exchange_rate=exchange_rate,
	)

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = payment_type
	pe.company = company
	pe.posting_date = getdate(posting_date)
	pe.party_type = party_type
	pe.party = party
	pe.reference_no = reference_no
	pe.reference_date = getdate(reference_date)

	bank_side = {
		"account": bank_account_head,
		"currency": amounts.bank_currency,
		"type": frappe.get_cached_value("Account", bank_account_head, "account_type"),
		"amount": amounts.bank_amount,
		"rate": amounts.bank_rate,
	}
	party_side = {
		"account": amounts.party_account,
		"currency": amounts.party_currency,
		"type": frappe.get_cached_value("Account", amounts.party_account, "account_type"),
		"amount": amounts.party_amount,
		"rate": amounts.party_rate,
	}
	source, target = (bank_side, party_side) if payment_type == "Pay" else (party_side, bank_side)

	pe.paid_from = source["account"]
	pe.paid_from_account_currency = source["currency"]
	pe.paid_from_account_type = source["type"]
	pe.paid_amount = source["amount"]
	pe.source_exchange_rate = source["rate"]
	pe.paid_to = target["account"]
	pe.paid_to_account_currency = target["currency"]
	pe.paid_to_account_type = target["type"]
	pe.received_amount = target["amount"]
	pe.target_exchange_rate = target["rate"]

	for ref in _allocate_references(references, amounts.party_amount, amounts.precision):
		pe.append("references", ref)

	if remarks:
		pe.custom_remarks = 1
		pe.remarks = remarks

	_set_treasury_fields(pe, source_doctype, source_name, event)
	pe.flags.ignore_permissions = True
	pe.insert()
	if submit:
		pe.submit()
	return pe.name


def _get_payment_amounts(
	*,
	company: str,
	posting_date,
	party_type: str,
	party: str,
	amount: float,
	bank_account_head: str,
	currency: str | None,
	exchange_rate: float | None,
) -> frappe._dict:
	"""Split a cheque amount into the bank side and the party side of a Payment Entry."""
	precision = frappe.get_precision("Payment Entry", "paid_amount")
	company_currency = erpnext.get_company_currency(company)
	bank_currency = get_account_currency(bank_account_head)
	if currency and currency != bank_currency:
		frappe.throw(
			_("Currency {0} does not match the currency {1} of account {2}.").format(
				currency, bank_currency, frappe.bold(bank_account_head)
			)
		)

	party_account = get_party_account(party_type, party, company)
	if not party_account:
		frappe.throw(_("No party account found for {0} {1}.").format(_(party_type), frappe.bold(party)))
	party_currency = get_account_currency(party_account)

	if bank_currency == company_currency:
		exchange_rate = 1.0
	elif not exchange_rate:
		exchange_rate = get_exchange_rate(bank_currency, company_currency, posting_date)
	if not flt(exchange_rate):
		frappe.throw(_("Exchange rate from {0} to {1} is required.").format(bank_currency, company_currency))

	bank_amount = flt(amount, precision)
	if bank_amount <= 0:
		frappe.throw(_("Amount must be greater than zero."))

	if party_currency == bank_currency:
		party_amount, party_rate = bank_amount, flt(exchange_rate)
	elif party_currency == company_currency:
		party_amount, party_rate = flt(bank_amount * flt(exchange_rate), precision), 1.0
	else:
		frappe.throw(
			_("Party account currency {0} must be either {1} or the company currency {2}.").format(
				party_currency, bank_currency, company_currency
			)
		)

	return frappe._dict(
		precision=precision,
		bank_currency=bank_currency,
		bank_amount=bank_amount,
		bank_rate=flt(exchange_rate),
		party_account=party_account,
		party_currency=party_currency,
		party_amount=party_amount,
		party_rate=party_rate,
	)


def _allocate_references(references: list[dict] | None, party_amount: float, precision: int) -> list[dict]:
	"""Trim requested allocations to the current outstanding and to the payment amount.

	Whatever cannot be allocated stays unallocated (advance) and the user is warned.
	"""
	allocations = []
	remaining = flt(party_amount, precision)
	dropped = []

	for ref in references or []:
		requested = flt(ref.get("allocated_amount"), precision)
		doctype, name = ref.get("reference_doctype"), ref.get("reference_name")
		allowed = requested
		if doctype in INVOICE_DOCTYPES:
			invoice = frappe.db.get_value(doctype, name, ["docstatus", "outstanding_amount"], as_dict=True)
			outstanding = flt(invoice.outstanding_amount, precision) if invoice and invoice.docstatus == 1 else 0
			allowed = min(requested, max(outstanding, 0))
		allowed = min(allowed, remaining)

		if allowed < requested:
			dropped.append(name)
		if allowed <= 0:
			continue

		allocations.append(
			{"reference_doctype": doctype, "reference_name": name, "allocated_amount": allowed}
		)
		remaining = flt(remaining - allowed, precision)

	if dropped:
		frappe.msgprint(
			_(
				"The following references are no longer outstanding enough and were reduced or skipped: {0}. The difference is kept as an unallocated advance."
			).format(", ".join(frappe.bold(d) for d in dropped)),
			title=_("Allocation Adjusted"),
			indicator="orange",
		)
	return allocations


# ---------------------------------------------------------------------------
# Journal Entry
# ---------------------------------------------------------------------------


def create_journal_entry(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	company: str,
	posting_date,
	accounts: list[dict],
	cheque_no: str | None = None,
	cheque_date=None,
	clearance_date=None,
	remarks: str | None = None,
	multi_currency: bool = False,
	voucher_type: str = "Journal Entry",
	reversal_of: str | None = None,
	submit: bool = True,
) -> str:
	"""Create (or return the existing) Journal Entry for (source, event).

	`accounts` rows accept the Journal Entry Account fields (account, debit_in_account_currency,
	credit_in_account_currency, party_type, party, exchange_rate, reference_type, reference_name,
	cost_center, user_remark).
	"""
	if existing := find_voucher("Journal Entry", source_doctype, source_name, event):
		return _return_existing("Journal Entry", existing, submit)

	if not accounts:
		frappe.throw(_("At least one account row is required."))

	company_currency = erpnext.get_company_currency(company)
	je = frappe.new_doc("Journal Entry")
	je.voucher_type = voucher_type
	je.company = company
	je.posting_date = getdate(posting_date)
	je.cheque_no = cheque_no
	je.cheque_date = getdate(cheque_date) if cheque_date else None
	je.reversal_of = reversal_of
	if remarks:
		je.user_remark = remarks

	for row in accounts:
		row = dict(row)
		if get_account_currency(row["account"]) != company_currency:
			multi_currency = True
		je.append("accounts", row)
	je.multi_currency = 1 if multi_currency else 0

	_set_treasury_fields(je, source_doctype, source_name, event)
	je.flags.ignore_permissions = True
	je.insert()
	if submit:
		je.submit()
		if clearance_date:
			# JournalEntry.validate() clears clearance_date, so it can only be set after submit
			# (the same way the Bank Reconciliation Tool does).
			frappe.db.set_value("Journal Entry", je.name, "clearance_date", getdate(clearance_date))
	return je.name


# ---------------------------------------------------------------------------
# Cancellation and reversal
# ---------------------------------------------------------------------------


def cancel_voucher(doctype: str, name: str):
	"""Cancel (or delete, if still draft) a Treasury-managed voucher, bypassing the voucher guard."""
	if doctype not in VOUCHER_DOCTYPES:
		frappe.throw(_("Only Payment Entry and Journal Entry can be cancelled here."))

	doc = frappe.get_doc(doctype, name)
	previous = frappe.flags.treasury_allow_cancel
	frappe.flags.treasury_allow_cancel = True
	try:
		doc.flags.ignore_permissions = True
		if doc.docstatus == 1:
			doc.cancel()
		elif doc.docstatus == 0:
			frappe.delete_doc(doctype, name, ignore_permissions=True)
	finally:
		frappe.flags.treasury_allow_cancel = previous


def make_reversal(doctype: str, name: str, posting_date, event: str, remarks: str | None = None) -> str:
	"""Create a Journal Entry that mirrors a submitted voucher (debit <-> credit), linked to the same source."""
	original = frappe.get_doc(doctype, name)
	if original.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted to be reversed.").format(_(doctype), name))
	if not original.get("treasury_source_name"):
		frappe.throw(_("{0} {1} is not managed by Treasury.").format(_(doctype), name))

	if doctype == "Journal Entry":
		rows = _reverse_journal_rows(original)
	else:
		rows = _reverse_gl_rows(doctype, name)

	return create_journal_entry(
		source_doctype=original.treasury_source_doctype,
		source_name=original.treasury_source_name,
		event=event,
		company=original.company,
		posting_date=posting_date,
		accounts=rows,
		cheque_no=original.get("cheque_no") or original.get("reference_no"),
		cheque_date=original.get("cheque_date") or original.get("reference_date"),
		remarks=remarks or _("Reversal of {0} {1}").format(_(doctype), name),
		multi_currency=bool(original.get("multi_currency")),
		reversal_of=name if doctype == "Journal Entry" else None,
	)


def reverse_journal_entry(name: str, posting_date, event: str, remarks: str | None = None) -> str:
	"""Shortcut for `make_reversal("Journal Entry", ...)`."""
	return make_reversal("Journal Entry", name, posting_date, event, remarks)


def _reverse_journal_rows(je: Document) -> list[dict]:
	rows = []
	for d in je.accounts:
		row = {
			"account": d.account,
			"party_type": d.party_type,
			"party": d.party,
			"cost_center": d.cost_center,
			"exchange_rate": d.exchange_rate,
			"debit_in_account_currency": d.credit_in_account_currency,
			"credit_in_account_currency": d.debit_in_account_currency,
		}
		if d.reference_type in INVOICE_DOCTYPES:
			row.update(reference_type=d.reference_type, reference_name=d.reference_name)
		rows.append(row)
	return rows


def _reverse_gl_rows(voucher_type: str, voucher_no: str) -> list[dict]:
	"""Build reversal rows from the GL Entries of a voucher (used for Payment Entries)."""
	gl = frappe.qb.DocType("GL Entry")
	entries = (
		frappe.qb.from_(gl)
		.select(
			gl.account,
			gl.party_type,
			gl.party,
			gl.cost_center,
			gl.against_voucher_type,
			gl.against_voucher,
			gl.debit,
			gl.credit,
			gl.debit_in_account_currency,
			gl.credit_in_account_currency,
		)
		.where((gl.voucher_type == voucher_type) & (gl.voucher_no == voucher_no) & (gl.is_cancelled == 0))
		.orderby(gl.creation)
	).run(as_dict=True)

	rows = []
	for e in entries:
		amount_in_account_currency = flt(e.debit_in_account_currency) or flt(e.credit_in_account_currency)
		amount = flt(e.debit) or flt(e.credit)
		row = {
			"account": e.account,
			"party_type": e.party_type,
			"party": e.party,
			"cost_center": e.cost_center,
			"exchange_rate": (amount / amount_in_account_currency) if amount_in_account_currency else 1,
			"debit_in_account_currency": flt(e.credit_in_account_currency),
			"credit_in_account_currency": flt(e.debit_in_account_currency),
		}
		if e.against_voucher_type in INVOICE_DOCTYPES and e.against_voucher:
			row.update(reference_type=e.against_voucher_type, reference_name=e.against_voucher)
		rows.append(row)
	return rows
