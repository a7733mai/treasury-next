"""Treasury posting service — the only place that creates accounting vouchers.

Every voucher is a standard Payment Entry or Journal Entry linked back to its Treasury source through the
read-only custom fields `treasury_source_doctype`, `treasury_source_name`, `treasury_event` and
`treasury_cycle`. All creators are idempotent on (source_doctype, source_name, event, cycle): `cycle`
starts at 1 and callers increment it when the same event legitimately happens again on one source
(e.g. a bounced cheque that is re-presented posts `party_posting` again in cycle 2).

Permissions: the calling controller / lifecycle / tool checks the user's permission (and role) on the
Treasury source document first. The public creators below then build, insert, submit and cancel the ERPNext
vouchers inside `treasury.utils.context.elevated` (as Administrator): ERPNext checks the session user's own
permissions on the way (Account in `get_party_account`, the reference documents and the party of a Payment
Entry, ...), and Treasury users are not expected to hold any accounting permission — the app grants none.

Voucher owner: a voucher created here gets `owner` / `modified_by` = the acting user (`real_user()`, the
user who ran the Treasury action; Administrator for scheduler jobs) instead of Administrator, so the
voucher shows who caused it, like the Treasury status log. Its GL / Payment Ledger rows and its Version
history keep Administrator, the user the voucher was actually saved as.
"""

from functools import wraps

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.party import get_party_account as erpnext_get_party_account
from erpnext.setup.utils import get_exchange_rate
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Sum
from frappe.utils import cint, flt, getdate

from treasury.utils.context import elevated, real_user

VOUCHER_DOCTYPES = ("Payment Entry", "Journal Entry")
INTERNAL_TRANSFER = "Internal Transfer"
INVOICE_DOCTYPES = ("Sales Invoice", "Purchase Invoice")
ORDER_DOCTYPES = ("Sales Order", "Purchase Order")
REFERENCE_DOCTYPES = (*INVOICE_DOCTYPES, *ORDER_DOCTYPES, "Journal Entry")


def _system_context(fn):
	"""Run a posting service function inside `elevated` (see module docstring)."""

	@wraps(fn)
	def wrapper(*args, **kwargs):
		with elevated():
			return fn(*args, **kwargs)

	return wrapper


def _stamp_user(doctype: str, name: str, owner: bool = False):
	"""Record the acting user on a voucher saved as Administrator (see module docstring)."""
	user = real_user()
	if user == frappe.session.user:
		return
	values = {"modified_by": user}
	if owner:
		values["owner"] = user
	frappe.db.set_value(doctype, name, values, update_modified=False)


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


@_system_context
def get_party_account(party_type: str, party: str, company: str) -> str:
	"""ERPNext's party account of `party` in `company`, independent of the user's Account permission."""
	return erpnext_get_party_account(party_type, party, company)


def get_cheque_exchange_rate(currency: str, company: str, posting_date) -> float:
	"""Rate converting `currency` to the company currency on `posting_date` (1 for the company currency)."""
	company_currency = erpnext.get_company_currency(company)
	if not currency or currency == company_currency:
		return 1.0
	rate = flt(get_exchange_rate(currency, company_currency, getdate(posting_date)))
	if not rate:
		frappe.throw(_("Exchange rate from {0} to {1} is required.").format(currency, company_currency))
	return rate


# ---------------------------------------------------------------------------
# Linked vouchers
# ---------------------------------------------------------------------------


def get_linked_vouchers(source_doctype: str, source_name: str) -> list[dict]:
	"""All vouchers (any docstatus) created for a Treasury source, oldest first."""
	vouchers = []
	for doctype in VOUCHER_DOCTYPES:
		fields = ["name", "docstatus", "posting_date", "treasury_event", "treasury_cycle", "creation"]
		if doctype == "Payment Entry":
			fields.append("payment_type")
		rows = frappe.get_all(
			doctype,
			filters={"treasury_source_doctype": source_doctype, "treasury_source_name": source_name},
			fields=fields,
			order_by="creation asc",
		)
		for row in rows:
			row["doctype"] = doctype
			vouchers.append(row)
	vouchers.sort(key=lambda d: d["creation"])
	return vouchers


def get_voucher_rows(source_doctype: str, source_names: list[str]) -> list[dict]:
	"""All vouchers (any docstatus) of several sources of one doctype, oldest first, with the details a
	voucher summary shows; `amount` is the paid amount of a Payment Entry (in `currency`, its paid-from account
	currency), the total debit of a Journal Entry (company currency, `currency` None)."""
	if not source_names:
		return []
	common = [
		"name",
		"docstatus",
		"company",
		"posting_date",
		"treasury_event",
		"treasury_cycle",
		"treasury_source_name",
		"clearance_date",
		"creation",
	]
	specific = {
		"Payment Entry": [
			"reference_no",
			"payment_type",
			"mode_of_payment",
			"paid_amount as amount",
			"paid_from_account_currency as currency",
		],
		"Journal Entry": ["cheque_no as reference_no", "total_debit as amount"],
	}
	rows = []
	for doctype in VOUCHER_DOCTYPES:
		for row in frappe.get_all(
			doctype,
			filters={"treasury_source_doctype": source_doctype, "treasury_source_name": ["in", source_names]},
			fields=common + specific[doctype],
			order_by="creation asc",
		):
			row.doctype = doctype
			row.setdefault("payment_type", None)
			row.setdefault("mode_of_payment", None)
			row.setdefault("currency", None)
			rows.append(row)
	rows.sort(key=lambda d: d.creation)
	return rows


def find_voucher(
	doctype: str, source_doctype: str, source_name: str, event: str, cycle: int = 1
) -> str | None:
	"""Return the draft/submitted voucher already created for (source, event, cycle), if any."""
	names = frappe.get_all(
		doctype,
		filters={
			"treasury_source_doctype": source_doctype,
			"treasury_source_name": source_name,
			"treasury_event": event,
			"treasury_cycle": _validate_cycle(cycle),
			"docstatus": ["<", 2],
		},
		order_by="creation desc",
		limit=1,
		pluck="name",
	)
	return names[0] if names else None


def _validate_cycle(cycle: int) -> int:
	if cint(cycle) < 1:
		frappe.throw(_("Treasury cycle must be a positive whole number."))
	return cint(cycle)


def _lock_source(source_doctype: str, source_name: str):
	"""Lock the source row so concurrent calls (e.g. a double click) cannot both create a voucher."""
	if not frappe.db.get_value(source_doctype, source_name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(source_doctype), source_name))


def _return_existing(doctype: str, name: str, submit: bool) -> str:
	doc = frappe.get_doc(doctype, name)
	if submit and doc.docstatus == 0:
		doc.flags.ignore_permissions = True  # see module docstring
		doc.submit()
		_stamp_user(doctype, name)
	return name


def _set_treasury_fields(doc: Document, source_doctype: str, source_name: str, event: str, cycle: int):
	doc.treasury_source_doctype = source_doctype
	doc.treasury_source_name = source_name
	doc.treasury_event = event
	doc.treasury_cycle = cycle


# ---------------------------------------------------------------------------
# Payment Entry
# ---------------------------------------------------------------------------


@_system_context
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
	cycle: int = 1,
	submit: bool = True,
	clearance_date=None,
	details: dict | None = None,
) -> str:
	"""Create (or return the existing) Payment Entry whose bank side is `bank_account_head`.

	`amount` is expressed in the currency of `bank_account_head` (the cheque currency) and
	`exchange_rate` converts that currency to the company currency. `clearance_date` (the bank already
	moved the money) requires `submit=True`. `details`: see `_apply_details`.
	"""
	if clearance_date and not submit:
		frappe.throw(_("Clearance Date can only be set on a submitted Payment Entry."))

	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		_return_existing("Payment Entry", existing, submit)
		if clearance_date and not frappe.db.get_value("Payment Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date, "Payment Entry")
		return existing

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

	for ref in _allocate_references(
		references, party_type, party, amounts.party_currency, amounts.party_amount, amounts.precision
	):
		pe.append("references", ref)

	if remarks:
		pe.custom_remarks = 1
		pe.remarks = remarks

	_apply_details(pe, details)
	_set_treasury_fields(pe, source_doctype, source_name, event, cycle)
	pe.flags.ignore_permissions = True  # see module docstring
	pe.insert()
	if submit:
		pe.submit()
		if clearance_date:
			_set_clearance_date(pe.name, clearance_date, "Payment Entry")
	_stamp_user("Payment Entry", pe.name, owner=True)
	return pe.name


def _apply_details(pe: Document, details: dict | None):
	"""Accounting details of a Payment Entry, from its Treasury source.

	Keys: `naming_series` (must already be a Payment Entry series), `mode_of_payment` (ignored when missing or
	disabled: the settings validate it), `cost_center`, `project`, `bank_account` (the company Bank Account),
	`dimensions` ({fieldname: value}), and for Pay / Receive only `party_bank_account` and `contact_person`.
	"""
	if not details:
		return
	details = frappe._dict(details)
	series = (details.naming_series or "").strip()
	if series:
		if series not in frappe.get_meta("Payment Entry").get_naming_series_options():
			frappe.throw(
				_(
					"Series {0} is not a Payment Entry series. A System Manager must add it in Treasury Company Settings (Add Series to Payment Entry)."
				).format(frappe.bold(series)),
				title=_("Missing Settings"),
			)
		pe.naming_series = series
	if details.mode_of_payment and frappe.db.get_value("Mode of Payment", details.mode_of_payment, "enabled"):
		pe.mode_of_payment = details.mode_of_payment
	for fieldname in ("cost_center", "project", "bank_account"):
		if details.get(fieldname):
			pe.set(fieldname, details[fieldname])
	for fieldname, value in _dimension_values(pe.meta, details.dimensions).items():
		pe.set(fieldname, value)
	if pe.payment_type != INTERNAL_TRANSFER:
		for fieldname in ("party_bank_account", "contact_person"):
			if details.get(fieldname):
				pe.set(fieldname, details[fieldname])


def _dimension_values(meta, dimensions: dict | None) -> dict:
	"""The set values of `dimensions` whose fieldname is an enabled Accounting Dimension field of `meta`."""
	if not dimensions:
		return {}
	enabled = set(get_accounting_dimensions())
	return {f: v for f, v in dimensions.items() if v and f in enabled and meta.has_field(f)}


@_system_context
def create_internal_transfer(
	*,
	source_doctype: str,
	source_name: str,
	event: str,
	company: str,
	posting_date,
	paid_from: str,
	paid_to: str,
	paid_amount: float,
	reference_no: str,
	reference_date,
	received_amount: float | None = None,
	exchange_rate: float | None = None,
	deduction_account: str | None = None,
	remarks: str | None = None,
	details: dict | None = None,
	cycle: int = 1,
	submit: bool = True,
	clearance_date=None,
) -> str:
	"""Create (or return the existing) Internal Transfer Payment Entry `paid_from` → `paid_to`.

	Both accounts are in one currency; `paid_amount` / `received_amount` are in that currency and
	`exchange_rate` converts it to the company currency (one rate for both sides: ERPNext forces the target
	rate to the source rate when the currencies are equal). A `received_amount` below `paid_amount` books the
	difference (e.g. bank charges) on `deduction_account`. `clearance_date` requires `submit=True`.
	"""
	if clearance_date and not submit:
		frappe.throw(_("Clearance Date can only be set on a submitted Payment Entry."))

	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		_return_existing("Payment Entry", existing, submit)
		if clearance_date and not frappe.db.get_value("Payment Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date, "Payment Entry")
		return existing

	if paid_from == paid_to:
		frappe.throw(_("Paid From and Paid To accounts must be different for an Internal Transfer."))
	source, target = _transfer_account(paid_from, company), _transfer_account(paid_to, company)
	if source.account_currency != target.account_currency:
		frappe.throw(_("Internal Transfer between accounts in different currencies is not supported."))

	precision = frappe.get_precision("Payment Entry", "paid_amount")
	paid = flt(paid_amount, precision)
	received = flt(paid if received_amount is None else received_amount, precision)
	if paid <= 0 or received <= 0:
		frappe.throw(_("Amount must be greater than zero."))
	if received > paid:
		frappe.throw(_("Received Amount cannot be greater than Paid Amount"))

	company_currency = erpnext.get_company_currency(company)
	if source.account_currency == company_currency:
		rate = 1.0
	else:
		rate = flt(exchange_rate) or flt(
			get_exchange_rate(source.account_currency, company_currency, getdate(posting_date))
		)
	if rate <= 0:
		frappe.throw(
			_("Exchange rate from {0} to {1} is required.").format(source.account_currency, company_currency)
		)

	details = frappe._dict(details or {})
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = INTERNAL_TRANSFER
	pe.company = company
	pe.posting_date = getdate(posting_date)
	pe.paid_from = paid_from
	pe.paid_from_account_currency = source.account_currency
	pe.paid_from_account_type = source.account_type
	pe.paid_to = paid_to
	pe.paid_to_account_currency = target.account_currency
	pe.paid_to_account_type = target.account_type
	pe.paid_amount = paid
	pe.received_amount = received
	pe.source_exchange_rate = rate
	pe.target_exchange_rate = rate
	pe.reference_no = reference_no
	pe.reference_date = getdate(reference_date)
	if remarks:
		pe.custom_remarks = 1
		pe.remarks = remarks

	if received < paid:
		_append_difference_row(pe, company, deduction_account, flt((paid - received) * rate), details)

	_apply_details(pe, details)
	_set_treasury_fields(pe, source_doctype, source_name, event, cycle)
	pe.flags.ignore_permissions = True  # see module docstring
	pe.insert()
	if flt(pe.difference_amount):
		frappe.throw(
			_("The Internal Transfer {0} is not balanced (difference {1}).").format(pe.name, pe.difference_amount)
		)
	if submit:
		pe.submit()
		if clearance_date:
			_set_clearance_date(pe.name, clearance_date, "Payment Entry")
	_stamp_user("Payment Entry", pe.name, owner=True)
	return pe.name


def _transfer_account(account: str, company: str) -> frappe._dict:
	details = frappe.db.get_value(
		"Account", account, ["company", "is_group", "account_currency", "account_type"], as_dict=True
	)
	if not details or details.company != company:
		frappe.throw(_("Account {0} does not belong to company {1}.").format(frappe.bold(account), company))
	if details.is_group:
		frappe.throw(_("Account {0} is a group account.").format(frappe.bold(account)))
	return details


def _append_difference_row(pe: Document, company: str, account: str | None, amount: float, details):
	"""The deduction row of an Internal Transfer whose received amount is below the paid amount.

	ERPNext 16 (PaymentEntry.set_exchange_gain_loss, run on every validate for every payment type) keeps
	exactly one row flagged `is_exchange_gain_loss` and sets its amount to base_paid - base_received, then
	subtracts *all* deductions in set_difference_amount: an unflagged charges row would be counted twice and
	the entry could not be submitted ("Difference Amount must be zero"). The difference is therefore put on the
	flagged row itself, with its account changed to `account`; ERPNext keeps the account of that first
	flagged row and only rewrites its amount (spec 7a F1, guarded by the PE settlement tests).
	"""
	if not account:
		frappe.throw(_("An account for the difference between the paid and the received amount is required."))
	company_currency = erpnext.get_company_currency(company)
	account_details = _transfer_account(account, company)
	if account_details.account_currency != company_currency:
		frappe.throw(
			_("Account {0} must be in the company currency {1}.").format(frappe.bold(account), company_currency)
		)
	row = {
		"account": account,
		"cost_center": details.get("cost_center") or erpnext.get_default_cost_center(company),
		"amount": flt(amount, frappe.get_precision("Payment Entry Deduction", "amount")),
		"is_exchange_gain_loss": 1,
	}
	row.update(_dimension_values(frappe.get_meta("Payment Entry Deduction"), details.get("dimensions")))
	pe.append("deductions", row)


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


def _allocate_references(
	references: list[dict] | None,
	party_type: str,
	party: str,
	party_currency: str,
	party_amount: float,
	precision: int,
) -> list[dict]:
	"""Trim requested allocations to the current outstanding and to the payment amount.

	Amounts are in the party account currency. Whatever cannot be allocated stays unallocated (advance) and the user is warned.
	A document listed more than once is merged into one row (ERPNext rejects duplicate references), and its
	combined allocation never exceeds its outstanding.
	"""
	allocations: dict[tuple[str, str], dict] = {}
	remaining = flt(party_amount, precision)
	short_outstanding, over_payment = [], []

	for ref in references or []:
		doctype, name = ref.get("reference_doctype"), ref.get("reference_name")
		key = (doctype, name)
		requested = flt(ref.get("allocated_amount"), precision)
		outstanding = _get_reference_outstanding(doctype, name, party_type, party, party_currency, precision)
		if key in allocations:
			outstanding = max(flt(outstanding - allocations[key]["allocated_amount"], precision), 0)

		allowed = min(requested, outstanding)
		if allowed < requested and name not in short_outstanding:
			short_outstanding.append(name)
		if allowed > remaining:
			if name not in over_payment:
				over_payment.append(name)
			allowed = remaining
		if allowed <= 0:
			continue

		if key in allocations:
			allocations[key]["allocated_amount"] = flt(
				allocations[key]["allocated_amount"] + allowed, precision
			)
		else:
			allocations[key] = {
				"reference_doctype": doctype,
				"reference_name": name,
				"allocated_amount": allowed,
			}
		remaining = flt(remaining - allowed, precision)

	messages = []
	if short_outstanding:
		messages.append(
			_(
				"The following references are no longer outstanding enough and were reduced or skipped: {0}. The difference is kept as an unallocated advance."
			).format(", ".join(frappe.bold(d) for d in short_outstanding))
		)
	if over_payment:
		messages.append(
			_(
				"The following references were reduced or skipped because the payment amount is already fully allocated: {0}."
			).format(", ".join(frappe.bold(d) for d in over_payment))
		)
	if messages:
		frappe.msgprint("<br>".join(messages), title=_("Allocation Adjusted"), indicator="orange")
	return list(allocations.values())


def get_reference_outstanding(
	doctype: str, name: str, party_type: str, party: str, party_currency: str, precision: int
) -> float:
	"""Outstanding of a reference document in the party account currency, as the Payment Entry allocation
	will see it (used to validate allocations before any voucher exists, e.g. Cheque Batch)."""
	return _get_reference_outstanding(doctype, name, party_type, party, party_currency, precision)


def _get_reference_outstanding(
	doctype: str, name: str, party_type: str, party: str, party_currency: str, precision: int
) -> float:
	"""Outstanding of a reference document in the party account currency (0 if not submitted)."""
	if doctype not in REFERENCE_DOCTYPES:
		frappe.throw(_("Reference type {0} is not supported.").format(_(doctype)))
	if frappe.db.get_value(doctype, name, "docstatus") != 1:
		return 0

	if doctype in INVOICE_DOCTYPES:
		# ERPNext keeps invoice outstanding_amount in the party account currency (from the Payment Ledger).
		outstanding = frappe.db.get_value(doctype, name, "outstanding_amount")
	elif doctype in ORDER_DOCTYPES:
		outstanding = _get_order_outstanding(doctype, name, party_currency)
	else:
		outstanding = _get_ledger_outstanding(doctype, name, party_type, party)
	return max(flt(outstanding, precision), 0)


def _get_order_outstanding(doctype: str, name: str, party_currency: str) -> float:
	"""Order total not yet covered by advances (as ERPNext's `get_reference_details` computes it)."""
	fields = ["currency", "grand_total", "rounded_total", "base_grand_total", "base_rounded_total"]
	order = frappe.db.get_value(doctype, name, [*fields, "advance_paid"], as_dict=True)
	if order.currency == party_currency:
		total = flt(order.rounded_total) or flt(order.grand_total)
	else:
		total = flt(order.base_rounded_total) or flt(order.base_grand_total)
	return total - flt(order.advance_paid)


def _get_ledger_outstanding(voucher_type: str, voucher_no: str, party_type: str, party: str) -> float:
	"""Open amount (party account currency) of a voucher for a party, from the Payment Ledger."""
	ple = frappe.qb.DocType("Payment Ledger Entry")
	result = (
		frappe.qb.from_(ple)
		.select(Sum(ple.amount_in_account_currency))
		.where(
			(ple.against_voucher_type == voucher_type)
			& (ple.against_voucher_no == voucher_no)
			& (ple.party_type == party_type)
			& (ple.party == party)
			& (ple.delinked == 0)
		)
	).run()
	return flt(result[0][0]) if result else 0


# ---------------------------------------------------------------------------
# Journal Entry
# ---------------------------------------------------------------------------


@_system_context
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
	cycle: int = 1,
	submit: bool = True,
	balance_exchange_difference: bool = False,
) -> str:
	"""Create (or return the existing) Journal Entry for (source, event, cycle).

	`accounts` rows accept the Journal Entry Account fields (account, debit_in_account_currency,
	credit_in_account_currency, party_type, party, exchange_rate, reference_type, reference_name,
	cost_center, user_remark). `clearance_date` requires `submit=True`.

	With `balance_exchange_difference`, rows booked at different exchange rates (e.g. a foreign currency
	cheque carried at the party posting rate and cleared at today's rate) are balanced in the company
	currency by a row on the company's Exchange Gain / Loss account: a Journal Entry does not book that
	difference by itself, it rejects the unbalanced entry.
	"""
	if clearance_date and not submit:
		frappe.throw(_("Clearance Date can only be set on a submitted Journal Entry."))

	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Journal Entry", source_doctype, source_name, event, cycle):
		_return_existing("Journal Entry", existing, submit)
		if clearance_date and not frappe.db.get_value("Journal Entry", existing, "clearance_date"):
			_set_clearance_date(existing, clearance_date)
		return existing

	if not accounts:
		frappe.throw(_("At least one account row is required."))

	if balance_exchange_difference and (row := _exchange_difference_row(company, accounts)):
		accounts = [*accounts, row]

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

	_set_treasury_fields(je, source_doctype, source_name, event, cycle)
	je.flags.ignore_permissions = True  # see module docstring
	je.insert()
	if submit:
		je.submit()
		if clearance_date:
			_set_clearance_date(je.name, clearance_date)
	_stamp_user("Journal Entry", je.name, owner=True)
	return je.name


def _exchange_difference_row(company: str, accounts: list[dict]) -> dict | None:
	"""Row on the Exchange Gain / Loss account that balances `accounts` in the company currency, if needed.

	Company-currency amounts are rounded per row exactly as JournalEntry.set_amounts_in_company_currency does.
	"""
	precision = frappe.get_precision("Journal Entry Account", "debit")
	company_currency = erpnext.get_company_currency(company)
	difference = 0.0
	for row in accounts:
		if get_account_currency(row["account"]) == company_currency:
			rate = 1.0
		else:
			rate = flt(row.get("exchange_rate"))
			if not rate:
				frappe.throw(
					_("Exchange rate is required for account {0}.").format(frappe.bold(row["account"]))
				)
		debit = flt(flt(row.get("debit_in_account_currency")) * rate, precision)
		credit = flt(flt(row.get("credit_in_account_currency")) * rate, precision)
		difference += debit - credit

	difference = flt(difference, precision)
	if not difference:
		return None

	account = frappe.get_cached_value("Company", company, "exchange_gain_loss_account")
	if not account:
		frappe.throw(
			_("Set the Exchange Gain / Loss Account in company {0}.").format(frappe.bold(company)),
			title=_("Missing Settings"),
		)
	row = {"account": account, "cost_center": erpnext.get_default_cost_center(company)}
	if difference > 0:
		row["credit_in_account_currency"] = difference
	else:
		row["debit_in_account_currency"] = -difference
	return row


def _set_clearance_date(name: str, clearance_date, doctype: str = "Journal Entry"):
	# JournalEntry.validate() clears clearance_date, so it can only be set after submit
	# (the same way the Bank Reconciliation Tool does); a Payment Entry is cleared the same way.
	frappe.db.set_value(doctype, name, "clearance_date", getdate(clearance_date))


# ---------------------------------------------------------------------------
# Cancellation and reversal
# ---------------------------------------------------------------------------


@_system_context
def cancel_voucher(doctype: str, name: str):
	"""Cancel (or delete, if still draft) a Treasury-managed voucher, bypassing the voucher guard."""
	if doctype not in VOUCHER_DOCTYPES:
		frappe.throw(_("Only Payment Entry and Journal Entry can be cancelled here."))

	doc = frappe.get_doc(doctype, name)
	previous = frappe.flags.treasury_allow_cancel
	frappe.flags.treasury_allow_cancel = True
	try:
		doc.flags.ignore_permissions = True  # see module docstring
		if doc.docstatus == 1:
			doc.cancel()
			_stamp_user(doctype, name)
		elif doc.docstatus == 0:
			frappe.delete_doc(doctype, name, ignore_permissions=True)  # see module docstring
	finally:
		frappe.flags.treasury_allow_cancel = previous


@_system_context
def make_reversal(
	doctype: str, name: str, posting_date, event: str, remarks: str | None = None, cycle: int = 1
) -> str:
	"""Create a Journal Entry that mirrors a submitted voucher (debit <-> credit), linked to the same source."""
	original = frappe.get_doc(doctype, name)
	if original.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted to be reversed.").format(_(doctype), name))
	if not original.get("treasury_source_name"):
		frappe.throw(_("{0} {1} is not managed by Treasury.").format(_(doctype), name))

	source_doctype, source_name = original.treasury_source_doctype, original.treasury_source_name
	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Journal Entry", source_doctype, source_name, event, cycle):
		return _return_existing("Journal Entry", existing, True)

	if doctype == "Journal Entry":
		rows = _reverse_journal_rows(original)
	else:
		rows = _reverse_gl_rows(doctype, name)

	return create_journal_entry(
		source_doctype=source_doctype,
		source_name=source_name,
		event=event,
		company=original.company,
		posting_date=posting_date,
		accounts=rows,
		cheque_no=original.get("cheque_no") or original.get("reference_no"),
		cheque_date=original.get("cheque_date") or original.get("reference_date"),
		remarks=remarks or _("Reversal of {0} {1}").format(_(doctype), name),
		multi_currency=bool(original.get("multi_currency")),
		reversal_of=name if doctype == "Journal Entry" else None,
		cycle=cycle,
	)


def reverse_journal_entry(
	name: str, posting_date, event: str, remarks: str | None = None, cycle: int = 1
) -> str:
	"""Shortcut for `make_reversal("Journal Entry", ...)`."""
	return make_reversal("Journal Entry", name, posting_date, event, remarks, cycle)


def can_reverse_by_payment_entry(pe: Document) -> bool:
	"""A submitted Treasury Internal Transfer without deductions: its mirror is an Internal Transfer too.

	A party Payment Entry cannot be reversed by a Payment Entry (ERPNext rejects a reference to another
	Payment Entry, and a Payment Entry cannot re-open an invoice), and a negative exchange gain / loss row is
	not supported: those are reversed by a Journal Entry.
	"""
	return bool(
		pe.docstatus == 1
		and pe.get("treasury_source_name")
		and pe.payment_type == INTERNAL_TRANSFER
		and not pe.get("deductions")
	)


@_system_context
def make_reverse_payment_entry(
	name: str, posting_date, event: str, remarks: str | None = None, cycle: int = 1
) -> str:
	"""Create the Internal Transfer that mirrors the Internal Transfer `name` (same source, accounts swapped,
	same rate, series, mode and accounting details). No clearance date."""
	original = frappe.get_doc("Payment Entry", name)
	if not can_reverse_by_payment_entry(original):
		frappe.throw(_("Payment Entry {0} cannot be reversed by a Payment Entry.").format(frappe.bold(name)))

	source_doctype, source_name = original.treasury_source_doctype, original.treasury_source_name
	cycle = _validate_cycle(cycle)
	_lock_source(source_doctype, source_name)
	if existing := find_voucher("Payment Entry", source_doctype, source_name, event, cycle):
		return _return_existing("Payment Entry", existing, True)

	series = original.naming_series
	details = {
		# a series removed from Payment Entry since then must not block the reversal
		"naming_series": series
		if series in frappe.get_meta("Payment Entry").get_naming_series_options()
		else None,
		"mode_of_payment": original.mode_of_payment,
		"cost_center": original.cost_center,
		"project": original.project,
		"bank_account": original.bank_account,
		"dimensions": {
			f: original.get(f) for f in get_accounting_dimensions() if original.meta.has_field(f)
		},
	}
	return create_internal_transfer(
		source_doctype=source_doctype,
		source_name=source_name,
		event=event,
		company=original.company,
		posting_date=posting_date,
		paid_from=original.paid_to,
		paid_to=original.paid_from,
		paid_amount=original.received_amount,
		exchange_rate=original.source_exchange_rate,
		reference_no=original.reference_no,
		reference_date=original.reference_date,
		remarks=remarks or _("Reversal of Payment Entry {0}").format(name),
		details=details,
		cycle=cycle,
	)


@_system_context
def reverse_voucher(
	doctype: str, name: str, posting_date, event: str, remarks: str | None = None, cycle: int = 1
) -> tuple[str, str]:
	"""Reverse a Treasury voucher with the fitting voucher: an Internal Transfer without deductions by a
	reverse Payment Entry, anything else by a Journal Entry. Returns (doctype, name) of the reversal; the one
	already created for (source, event, cycle), of either doctype, is returned as it is."""
	if doctype not in VOUCHER_DOCTYPES:
		frappe.throw(_("Only Payment Entries and Journal Entries can be reversed here."))
	source = frappe.db.get_value(
		doctype, name, ["treasury_source_doctype", "treasury_source_name"], as_dict=True
	)
	if source and source.treasury_source_name:
		for voucher_doctype in VOUCHER_DOCTYPES:
			existing = find_voucher(
				voucher_doctype, source.treasury_source_doctype, source.treasury_source_name, event, cycle
			)
			if existing:
				return voucher_doctype, _return_existing(voucher_doctype, existing, True)

	if doctype == "Payment Entry" and can_reverse_by_payment_entry(frappe.get_doc(doctype, name)):
		return "Payment Entry", make_reverse_payment_entry(name, posting_date, event, remarks, cycle)
	return "Journal Entry", make_reversal(doctype, name, posting_date, event, remarks, cycle)


def _reverse_journal_rows(je: Document) -> list[dict]:
	rows = []
	dimensions = _row_dimension_fields("Journal Entry Account")
	for d in je.accounts:
		row = {
			"account": d.account,
			"party_type": d.party_type,
			"party": d.party,
			"cost_center": d.cost_center,
			"exchange_rate": d.exchange_rate,
			"debit_in_account_currency": d.credit_in_account_currency,
			"credit_in_account_currency": d.debit_in_account_currency,
			**{f: d.get(f) for f in dimensions if d.get(f)},
		}
		if d.reference_type in INVOICE_DOCTYPES:
			row.update(reference_type=d.reference_type, reference_name=d.reference_name)
		rows.append(row)
	return rows


def _reverse_gl_rows(voucher_type: str, voucher_no: str) -> list[dict]:
	"""Build reversal rows from the GL Entries of a voucher (used for Payment Entries)."""
	gl = frappe.qb.DocType("GL Entry")
	# accounting dimensions that both GL Entry and Journal Entry Account carry are kept on the reversal
	dimensions = [
		f for f in _row_dimension_fields("Journal Entry Account") if frappe.get_meta("GL Entry").has_field(f)
	]
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
			*(getattr(gl, f) for f in dimensions),
		)
		.where((gl.voucher_type == voucher_type) & (gl.voucher_no == voucher_no) & (gl.is_cancelled == 0))
		.orderby(gl.creation)
	).run(as_dict=True)

	order_advances = _get_order_advances(voucher_type, voucher_no)
	rows = []
	for e in entries:
		if e.against_voucher_type == "Journal Entry":
			# ERPNext's Journal Entry validation rejects a reference back to a Journal Entry on this row
			# (journal_entry.py validate_against_jv), so the reversal could not settle it.
			frappe.throw(
				_(
					"{0} {1} is allocated against Journal Entry {2} and cannot be reversed by a Journal Entry. Cancel it instead."
				).format(_(voucher_type), voucher_no, frappe.bold(e.against_voucher)),
				title=_("Reversal Not Possible"),
			)
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
			**{f: e.get(f) for f in dimensions if e.get(f)},
		}
		if e.against_voucher_type in INVOICE_DOCTYPES and e.against_voucher:
			row.update(reference_type=e.against_voucher_type, reference_name=e.against_voucher)
		elif (
			e.party_type
			and voucher_type == "Payment Entry"
			and e.against_voucher_type in (None, "", "Payment Entry")
		):
			# Unallocated and order-allocated parts: the Payment Ledger books both against the Payment Entry
			# itself, so match the reversal against it too; otherwise both would stay open.
			row.update(reference_type="Payment Entry", reference_name=e.against_voucher or voucher_no)
			rows.extend(_split_order_advances(row, order_advances))
			continue
		rows.append(row)
	return rows


def _row_dimension_fields(doctype: str) -> list[str]:
	"""Enabled Accounting Dimension fieldnames present on `doctype`."""
	meta = frappe.get_meta(doctype)
	return [f for f in get_accounting_dimensions() if meta.has_field(f)]


def _get_order_advances(voucher_type: str, voucher_no: str) -> list[dict]:
	"""Sales / Purchase Order allocations of a Payment Entry (party account currency), oldest first.

	GL Entry does not store the order (`advance_voucher_*` only exist on the voucher rows and on the Advance
	Payment Ledger Entry), so the orders are read from the Payment Entry's own references.
	"""
	if voucher_type != "Payment Entry":
		return []
	return [
		{"doctype": d.reference_doctype, "name": d.reference_name, "remaining": flt(d.allocated_amount)}
		for d in frappe.get_all(
			"Payment Entry Reference",
			filters={
				"parenttype": "Payment Entry",
				"parent": voucher_no,
				"reference_doctype": ["in", ORDER_DOCTYPES],
			},
			fields=["reference_doctype", "reference_name", "allocated_amount"],
			order_by="idx asc",
		)
		if flt(d.allocated_amount) > 0
	]


def _split_order_advances(row: dict, order_advances: list[dict]) -> list[dict]:
	"""Split a reversal party row into one row per order advance it covers, plus the unallocated rest.

	Each order row carries `advance_voucher_type` / `advance_voucher_no`, so ERPNext books an opposite
	Advance Payment Ledger Entry and recomputes the order's `advance_paid` (as it does for a Journal Entry
	row referencing an order). `order_advances` is consumed so a later party row does not reuse it.
	"""
	precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency")
	debit = flt(row["debit_in_account_currency"])
	side = "debit_in_account_currency" if debit else "credit_in_account_currency"
	left = flt(row[side], precision)
	rows = []
	for order in order_advances:
		take = flt(min(order["remaining"], left), precision)
		if take <= 0:
			continue
		rows.append(
			{
				**row,
				"debit_in_account_currency": 0,
				"credit_in_account_currency": 0,
				side: take,
				"advance_voucher_type": order["doctype"],
				"advance_voucher_no": order["name"],
			}
		)
		order["remaining"] = flt(order["remaining"] - take, precision)
		left = flt(left - take, precision)
	if left > 0 or not rows:
		rows.append({**row, "debit_in_account_currency": 0, "credit_in_account_currency": 0, side: left})
	return rows
