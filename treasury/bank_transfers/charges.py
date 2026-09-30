"""Bank Charge lifecycle: statuses, the voucher each charge posts and its reconciliation (spec 7d §3.3, §4).

Submit posts one voucher through treasury.accounting.posting, dated the posting date and keyed
(Bank Charge, name, bank_charge, 1), in the shape of the company's Bank Charge Voucher setting:
- Payment Entry (default): Pay to the Bank's Supplier (Treasury Settings → Bank Suppliers, created on demand)
  with the whole amount as a deduction on the expense account, so the supplier balance is untouched; a refund
  is a Receive with a negative deduction;
- Journal Entry: a Bank Entry, Dr Expense / Cr Bank (a refund: the reverse).
The voucher is cleared on the posting date (Bank Charge Clearance "On Posting"), or on the date of the bank
statement line it is reconciled with (PDC Bank Clearing, the Reconcile action, or a statement line set on the
draft). A refund updates the refunded amount of the charge it refunds. Cancel cancels the voucher (ERPNext
un-reconciles its statement line).

Only this module changes the statuses, voucher links, lifecycle dates and the status log of a submitted
charge (in place, see `_write_in_place`); the controller rejects any other change to them. Status log events
are stored in English and translated when displayed.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import cint, flt, fmt_money, getdate

from treasury.accounting import dimensions, payment_modes, posting
from treasury.bank_facilities import accounting as facility_accounting
from treasury.bank_facilities import instruments
from treasury.bank_transfers import bank_supplier
from treasury.bank_transfers.lifecycle import _bank_gl, _cancel_voucher, _write_in_place, log
from treasury.treasury_setup.doctype.bank_charge_category.bank_charge_category import get_account_row
from treasury.utils.bank_reconciliation import reconcile_voucher

DOCTYPE = "Bank Charge"
EVENT_CHARGE = "bank_charge"
CHARGE, REFUND = "Charge", "Refund"
DRAFT, POSTED, RECONCILED, CANCELLED = "Draft", "Posted", "Reconciled", "Cancelled"
NOT_POSTED = "Not Posted"
USER_ROLES = ("Treasury User", "Treasury Manager", "System Manager")
MANAGER_ROLES = ("Treasury Manager", "System Manager")
MODE_METHOD = "Bank Charge"
PAYMENT_ENTRY, JOURNAL_ENTRY = "Payment Entry", "Journal Entry"
ON_POSTING = "On Posting"
BANK_TRANSACTION = "Bank Transaction"
OPEN_TRANSACTION_STATUSES = ("Pending", "Unreconciled")
DESCRIPTION_LENGTH = 500
REFERENCE_LENGTH = 140


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _begin(name: str, roles: tuple[str, ...] | None = None, ptype: str = "write"):
	"""Lock the charge row, reload it and check the user's permission and role."""
	if not name or not frappe.db.get_value(DOCTYPE, name, "name", for_update=True):
		frappe.throw(_("{0} {1} does not exist").format(_(DOCTYPE), name), frappe.DoesNotExistError)
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.docstatus != 1:
		frappe.throw(_("{0} {1} must be submitted first.").format(_(DOCTYPE), frappe.bold(name)))
	doc.check_permission(ptype)
	_check_roles(roles)
	return doc


def _check_roles(roles: tuple[str, ...] | None):
	if roles and not set(roles) & set(frappe.get_roles()):
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in roles)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def _result(doc) -> dict:
	return {"name": doc.name, "status": doc.status, "accounting_status": doc.accounting_status}


def voucher_of(doc) -> tuple[str, str] | None:
	"""(doctype, name) of the charge's voucher, if posted."""
	if doc.get("payment_entry"):
		return (PAYMENT_ENTRY, doc.payment_entry)
	if doc.get("journal_entry"):
		return (JOURNAL_ENTRY, doc.journal_entry)
	return None


def _details(doc) -> dict:
	mode = payment_modes.resolve(doc.company, doc.bank_account, MODE_METHOD)
	return {
		"mode_of_payment": doc.mode_of_payment or mode.mode_of_payment,
		"naming_series": mode.naming_series,
		"cost_center": doc.cost_center or erpnext.get_default_cost_center(doc.company),
		"project": doc.project,
		"bank_account": doc.bank_account,
		"dimensions": dimensions.get_dimension_values(doc),
	}


def _remarks(doc) -> str:
	template = _("Bank Charge {0}: {1}") if doc.entry_type == CHARGE else _("Bank Charge Refund {0}: {1}")
	remarks = template.format(doc.name, _(doc.category))
	if doc.description:
		remarks += " " + doc.description
	if doc.remarks:
		remarks += "\n" + doc.remarks
	return remarks


def _je_rows(doc, bank_gl: str, pay: bool) -> list[dict]:
	"""Expense row (company currency, with the accounting details) and bank row (bank currency)."""
	precision = frappe.get_precision("Journal Entry Account", "debit") or 2
	rate = flt(doc.exchange_rate) or 1.0
	meta = frappe.get_meta("Journal Entry Account")
	expense = {
		"account": doc.expense_account,
		("debit_in_account_currency" if pay else "credit_in_account_currency"): flt(
			flt(doc.amount) * rate, precision
		),
		"cost_center": doc.cost_center or erpnext.get_default_cost_center(doc.company),
		"project": doc.project,
	}
	for fieldname, value in dimensions.get_dimension_values(doc).items():
		if value and meta.has_field(fieldname):
			expense[fieldname] = value
	side = {"credit": doc.amount} if pay else {"debit": doc.amount}
	bank = facility_accounting._row(doc.company, doc.currency, bank_gl, rate, **side)
	return [expense, bank]


# ---------------------------------------------------------------------------
# Submit, cancel (controller hooks)
# ---------------------------------------------------------------------------


def on_submit(doc):
	first_new_row = len(doc.status_log)
	post(doc)
	_write_in_place(
		doc,
		first_new_row,
		("payment_entry", "journal_entry", "clearance_date", "bank_transaction_date"),
	)


def post(doc):
	"""Post the charge's voucher (idempotent on the posting key), reconcile it with the statement line set on
	the charge, if any, and log the events; a refund updates the charge it refunds."""
	settings = posting.get_company_settings(doc.company)
	bank_gl = _bank_gl(doc.bank_account)
	pay = doc.entry_type == CHARGE
	bt = None
	if doc.bank_transaction:
		bt = validate_bank_transaction(doc, doc.bank_transaction, submitted=True)
		clearance = getdate(bt.date)
	elif (settings.bank_charge_clearance or ON_POSTING) == ON_POSTING:
		clearance = getdate(doc.posting_date)
	else:
		clearance = None

	common = {
		"source_doctype": DOCTYPE,
		"source_name": doc.name,
		"event": EVENT_CHARGE,
		"cycle": 1,
		"company": doc.company,
		"posting_date": doc.posting_date,
		"clearance_date": clearance,
		"remarks": _remarks(doc),
	}
	if settings.bank_charge_voucher == JOURNAL_ENTRY:
		journal = posting.create_journal_entry(
			**common,
			voucher_type="Bank Entry",
			accounts=_je_rows(doc, bank_gl, pay),
			cheque_no=doc.bank_reference or doc.name,
			cheque_date=doc.posting_date,
			balance_exchange_difference=True,
		)
		doc.journal_entry = journal
		voucher = (JOURNAL_ENTRY, journal)
	else:
		bank = frappe.db.get_value("Bank Account", doc.bank_account, "bank")
		if not bank:
			frappe.throw(
				_(
					"Bank Account {0} has no Bank: set its Bank, or book bank charges by Journal Entry (Treasury Company Settings)."
				).format(frappe.bold(doc.bank_account))
			)
		supplier = bank_supplier.ensure_bank_supplier(bank)
		payment_entry = posting.create_bank_charge_payment(
			**common,
			payment_type="Pay" if pay else "Receive",
			party_type="Supplier",
			party=supplier,
			amount=doc.amount,
			bank_account_head=bank_gl,
			expense_account=doc.expense_account,
			currency=doc.currency,
			exchange_rate=doc.exchange_rate,
			reference_no=doc.bank_reference or doc.name,
			reference_date=doc.posting_date,
			details=_details(doc),
		)
		doc.payment_entry = payment_entry
		voucher = (PAYMENT_ENTRY, payment_entry)

	log(doc, "Posted", POSTED, POSTED, voucher, event_date=doc.posting_date)
	if bt:
		reconcile_voucher(doc.bank_transaction, *voucher)
		# ERPNext may leave the voucher uncleared (or clear it on another date)
		posting.set_clearance_date(*voucher, bt.date)
		doc.bank_transaction_date = getdate(bt.date)
		log(doc, "Reconciled", RECONCILED, voucher=voucher, event_date=bt.date)
	doc.clearance_date = frappe.db.get_value(voucher[0], voucher[1], "clearance_date")

	if doc.entry_type == REFUND and doc.against_bank_charge:
		update_refunds(doc.against_bank_charge, doc, "Refunded")


def before_cancel(doc):
	if doc.entry_type != CHARGE:
		return
	refunds = submitted_refunds(doc.name)
	if refunds:
		frappe.throw(
			_("Bank Charge {0} has submitted refunds ({1}); cancel them first.").format(
				frappe.bold(doc.name), ", ".join(r.name for r in refunds)
			),
			title=_("Not Allowed"),
		)


def on_cancel(doc):
	"""Cancel the charge's voucher (before Frappe's check for submitted back links); ERPNext removes it from
	its Bank Transaction, whose line is open again."""
	first_new_row = len(doc.status_log)
	voucher = voucher_of(doc)
	if voucher and frappe.db.get_value(voucher[0], voucher[1], "docstatus") == 1:
		_cancel_voucher(*voucher)
	log(doc, "Cancelled", CANCELLED, NOT_POSTED, voucher=voucher)
	_write_in_place(doc, first_new_row)
	if doc.entry_type == REFUND and doc.against_bank_charge:
		update_refunds(doc.against_bank_charge, doc, "Refund Cancelled")


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------


def submitted_refunds(charge_name: str, exclude: str | None = None) -> list[frappe._dict]:
	filters = {"against_bank_charge": charge_name, "entry_type": REFUND, "docstatus": 1}
	if exclude:
		filters["name"] = ["!=", exclude]
	return frappe.get_all(DOCTYPE, filters=filters, fields=["name", "amount"], order_by="creation asc")


def update_refunds(charge_name: str, refund_doc, event: str):
	"""Recompute the refunded amount of a charge from its submitted refunds and log the refund `event`."""
	frappe.db.get_value(DOCTYPE, charge_name, "name", for_update=True)
	charge = frappe.get_doc(DOCTYPE, charge_name)
	precision = charge.precision("refunded_amount")
	refunded = flt(sum(flt(r.amount) for r in submitted_refunds(charge_name)), precision)
	if refunded > flt(charge.amount, precision):
		# two refunds submitted at the same time: the second one is rejected here, under the lock
		frappe.throw(
			_("Refunds of Bank Charge {0} cannot exceed its amount ({1} left).").format(
				frappe.bold(charge_name),
				fmt_money(
					max(flt(charge.amount) - (refunded - flt(refund_doc.amount)), 0), currency=charge.currency
				),
			)
		)
	first_new_row = len(charge.status_log)
	charge.refunded_amount = refunded
	log(
		charge,
		event,
		voucher=voucher_of(refund_doc),
		remarks=refund_doc.name,
		event_date=refund_doc.posting_date,
	)
	_write_in_place(charge, first_new_row, ("refunded_amount",))


# ---------------------------------------------------------------------------
# Bank transactions
# ---------------------------------------------------------------------------


def validate_bank_transaction(doc, bank_transaction: str, submitted: bool = False) -> frappe._dict:
	"""The statement line of a charge: an open transaction of its bank account, on its side (a charge is a
	withdrawal, a refund a deposit) with its open amount, and not used by another charge. Returns it."""
	bt = frappe.db.get_value(
		BANK_TRANSACTION,
		bank_transaction,
		[
			"name",
			"docstatus",
			"bank_account",
			"status",
			"date",
			"deposit",
			"withdrawal",
			"unallocated_amount",
			"currency",
		],
		as_dict=True,
	)
	if (
		not bt
		or bt.docstatus != 1
		or bt.bank_account != doc.bank_account
		or bt.status not in OPEN_TRANSACTION_STATUSES
	):
		frappe.throw(
			_("Bank Transaction {0} is not an open transaction of this bank account.").format(
				bank_transaction
			)
		)
	precision = frappe.get_precision(BANK_TRANSACTION, "unallocated_amount") or 2
	side = "withdrawal" if doc.entry_type == CHARGE else "deposit"
	if (
		flt(bt.get(side), precision) <= 0
		or flt(bt.unallocated_amount, precision) != flt(doc.amount, precision)
		or (bt.currency and doc.currency and bt.currency != doc.currency)
	):
		frappe.throw(
			_("The open amount of Bank Transaction {0} does not match Bank Charge {1}.").format(
				bank_transaction, doc.name
			)
		)
	filters = {"bank_transaction": bank_transaction, "docstatus": ["<", 2]}
	if doc.name:
		filters["name"] = ["!=", doc.name]
	other = frappe.get_all(DOCTYPE, filters=filters, pluck="name", limit=1)
	if other:
		frappe.throw(
			_("Bank Transaction {0} already has Bank Charge {1}.").format(bank_transaction, other[0])
		)
	return bt


def reconcile(name: str, bank_transaction: str) -> dict:
	"""Reconcile a Posted charge with its bank statement line (PDC Bank Clearing, the Reconcile action): the
	statement line is reconciled against the voucher, which is cleared on the statement date. No GL change."""
	doc = _begin(name, USER_ROLES)
	instruments.require_status(doc, (POSTED,), RECONCILED)
	if not bank_transaction:
		frappe.throw(_("Bank Transaction is required."))
	bt = validate_bank_transaction(doc, bank_transaction, submitted=True)
	voucher = voucher_of(doc)
	if not voucher:
		frappe.throw(_("Bank Charge {0} has no voucher to reconcile.").format(frappe.bold(doc.name)))
	reconcile_voucher(bank_transaction, *voucher)
	posting.set_clearance_date(*voucher, bt.date)

	first_new_row = len(doc.status_log)
	doc.bank_transaction = bank_transaction
	doc.bank_transaction_date = getdate(bt.date)
	doc.clearance_date = getdate(bt.date)
	log(doc, "Reconciled", RECONCILED, voucher=voucher, event_date=bt.date)
	# in place: `bank_transaction` is not an allow-on-submit field (it changes only here)
	_write_in_place(doc, first_new_row, ("bank_transaction", "bank_transaction_date", "clearance_date"))
	return _result(doc)


def create_from_bank_transaction(
	bank_transaction: str, category: str, company: str, bank_account: str
) -> str:
	"""Create and submit a Charge of `category` for an open withdrawal (PDC Bank Clearing): posted on the
	statement date for its open amount and reconciled with it on submit. The user's own permissions apply."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	frappe.has_permission(DOCTYPE, "submit", throw=True)
	_check_roles(USER_ROLES)
	if not category:
		frappe.throw(_("Choose a Charge Category."))

	bt = frappe.db.get_value(
		BANK_TRANSACTION,
		bank_transaction,
		[
			"name",
			"docstatus",
			"bank_account",
			"status",
			"date",
			"withdrawal",
			"unallocated_amount",
			"description",
			"reference_number",
		],
		as_dict=True,
		for_update=True,
	)
	existing = frappe.get_all(
		DOCTYPE,
		filters={"bank_transaction": bank_transaction, "docstatus": ["<", 2]},
		pluck="name",
		limit=1,
	)
	if existing:
		frappe.throw(
			_("Bank Transaction {0} already has Bank Charge {1}.").format(bank_transaction, existing[0])
		)
	if (
		not bt
		or bt.docstatus != 1
		or bt.bank_account != bank_account
		or bt.status not in OPEN_TRANSACTION_STATUSES
		or flt(bt.withdrawal) <= 0
		or flt(bt.unallocated_amount) <= 0
	):
		frappe.throw(
			_("Bank Transaction {0} is not an open withdrawal of this bank account.").format(bank_transaction)
		)
	details = frappe.db.get_value(
		"Bank Charge Category", category, ["disabled", "default_description"], as_dict=True
	)
	if details and cint(details.disabled):
		frappe.throw(_("Bank Charge Category {0} is disabled.").format(frappe.bold(category)))
	if not details or not get_account_row(category, company):
		frappe.throw(
			_("Set the Expense Account of Bank Charge Category {0} for company {1}.").format(
				frappe.bold(category), frappe.bold(company)
			)
		)

	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"entry_type": CHARGE,
			"company": company,
			"bank_account": bank_account,
			"category": category,
			"posting_date": bt.date,
			"amount": flt(bt.unallocated_amount),
			"description": (details.default_description or bt.description or "")[:DESCRIPTION_LENGTH] or None,
			"bank_reference": (bt.reference_number or "")[:REFERENCE_LENGTH] or None,
			"bank_transaction": bt.name,
		}
	)
	doc.insert()
	doc.submit()
	return doc.name
