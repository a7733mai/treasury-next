"""Bank Transfer helpers for the forms (read only: they never write).

- `make_bank_transfer`: a new, unsaved Bank Transfer for an invoice, order or party (the buttons on the ERPNext
  documents come in a later phase); the client opens it with `frappe.model.sync` + `set_route`.
- `get_fee_preview`: the scope, the fee of the matching rule and the limit messages of a transfer being
  entered, for the fee preview of the form; also the bank currencies, the party's default bank details, the
  method limit and whether the transfer needs a Treasury Manager's approval (Treasury roles cannot read
  Bank Account, so the form cannot look these up itself).
- `get_transfer_settings`: the company's Bank Transfer settings a new transfer form needs (defaults).
"""

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.utils import flt, getdate, today

from treasury.accounting import dimensions, posting
from treasury.bank_transfers import fees, lifecycle
from treasury.bank_transfers.doctype.bank_transfer.bank_transfer import transfer_settings
from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	TRANSFER_DEFAULT_BANK_FIELDS,
)
from treasury.utils.party import get_party_name

DOCTYPE = "Bank Transfer"
METHODS = ("Normal", "Fawran", "SWIFT")
# source doctype -> (transfer type, party type, party field)
SOURCES = {
	"Purchase Invoice": (lifecycle.PARTY_PAYMENT, "Supplier", "supplier"),
	"Purchase Order": (lifecycle.PARTY_PAYMENT, "Supplier", "supplier"),
	"Sales Invoice": (lifecycle.PARTY_RECEIPT, "Customer", "customer"),
	"Sales Order": (lifecycle.PARTY_RECEIPT, "Customer", "customer"),
	"Supplier": (lifecycle.PARTY_PAYMENT, "Supplier", None),
	"Customer": (lifecycle.PARTY_RECEIPT, "Customer", None),
	"Employee": (lifecycle.PARTY_PAYMENT, "Employee", None),
}
PARTY_MASTERS = ("Supplier", "Customer", "Employee")


@frappe.whitelist()
def make_bank_transfer(
	source_doctype: str, source_name: str, method: str | None = None, company: str | None = None
) -> dict:
	"""A new Bank Transfer (not inserted) paying / receiving the outstanding of an invoice or order, or for a
	party master (no amount). `company`: for a party master (default: the user's default company); a
	document's own company always wins."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	if source_doctype not in SOURCES:
		frappe.throw(_("Bank Transfer cannot be created from {0}.").format(_(source_doctype or "-")))
	if method and method not in METHODS:
		frappe.throw(_("Unknown method {0}.").format(frappe.bold(method)))
	source = frappe.get_doc(source_doctype, source_name)
	source.check_permission("read")
	transfer_type, party_type, party_field = SOURCES[source_doctype]
	company = source.get("company") or company or frappe.defaults.get_user_default("Company")
	settings = posting.get_company_settings(company)

	doc = frappe.new_doc(DOCTYPE)
	doc.company = company
	doc.transfer_type = transfer_type
	doc.method = method or settings.default_transfer_method or "Normal"
	doc.bank_account = settings.get(TRANSFER_DEFAULT_BANK_FIELDS[doc.method])
	doc.charge_bearer = settings.default_charge_bearer or fees.SHA
	doc.request_date = doc.execution_date = today()
	if doc.bank_account:
		gl_account = frappe.db.get_value("Bank Account", doc.bank_account, "account")
		doc.currency = get_account_currency(gl_account) if gl_account else None
		if doc.currency:
			doc.exchange_rate = posting.get_cheque_exchange_rate(doc.currency, company, doc.execution_date)
	doc.party_type = party_type
	doc.party = source.get(party_field) if party_field else source.name
	doc.party_name = get_party_name(party_type, doc.party)
	doc.party_bank_account = get_party_bank_account(party_type, doc.party)
	doc.contact_person = source.get("contact_person") or (
		get_default_contact(party_type, doc.party) if party_type != "Employee" else None
	)
	for fieldname in ("cost_center", "project"):
		if source.meta.has_field(fieldname) and source.get(fieldname):
			doc.set(fieldname, source.get(fieldname))
	dimensions.copy_dimensions(source, doc)
	if source_doctype not in PARTY_MASTERS:
		_set_amount(doc, source)
		label = _("Payment of {0} {1}") if transfer_type == lifecycle.PARTY_PAYMENT else _("Receipt of {0} {1}")
		doc.remarks = label.format(_(source_doctype), source_name)
	return doc.as_dict()


def _set_amount(doc, source):
	"""The outstanding of the invoice / order (party account currency), in the bank currency."""
	if source.docstatus != 1:
		frappe.throw(_("{0} {1} has nothing outstanding.").format(_(source.doctype), frappe.bold(source.name)))
	precision = frappe.get_precision(DOCTYPE, "amount") or 2
	party_account = posting.get_party_account(doc.party_type, doc.party, doc.company)
	party_currency = get_account_currency(party_account)
	outstanding = posting.get_reference_outstanding(
		source.doctype, source.name, doc.party_type, doc.party, party_currency, precision
	)
	if outstanding <= 0:
		frappe.throw(_("{0} {1} has nothing outstanding.").format(_(source.doctype), frappe.bold(source.name)))
	if party_currency == doc.currency or not doc.currency:
		doc.amount = outstanding
	elif party_currency == erpnext.get_company_currency(doc.company) and flt(doc.exchange_rate):
		doc.amount = flt(outstanding / flt(doc.exchange_rate), precision)
	doc.append(
		"references",
		{"reference_doctype": source.doctype, "reference_name": source.name, "allocated_amount": outstanding},
	)


def _check_prepare_permission():
	if not (frappe.has_permission(DOCTYPE, "create") or frappe.has_permission(DOCTYPE, "write")):
		frappe.throw(_("You are not permitted to prepare Bank Transfers."), frappe.PermissionError)


@frappe.whitelist()
def get_transfer_settings(company: str) -> dict:
	"""The Bank Transfer values of the company's Treasury Company Settings a new transfer form needs (default
	method, banks and charge bearer, completion, limit action, approval threshold); {} without settings."""
	_check_prepare_permission()
	if not company or not frappe.db.exists("Company", company):
		frappe.throw(_("Company {0} does not exist.").format(frappe.bold(company or "-")))
	return transfer_settings(company)


@frappe.whitelist()
def get_fee_preview(
	company: str,
	bank_account: str,
	method: str,
	transfer_type: str,
	amount=0,
	charge_bearer: str | None = None,
	party_bank_account: str | None = None,
	beneficiary_iban: str | None = None,
	beneficiary_bank: str | None = None,
	beneficiary_swift: str | None = None,
	to_bank_account: str | None = None,
	execution_date=None,
	party_type: str | None = None,
	party: str | None = None,
	name: str | None = None,
	exchange_rate=None,
) -> dict:
	"""What the transfer being entered will do, as the server will compute it on save (read only):
	{scope, computed_fee, correspondent_fee, fee_account, fee_rule, fee_basis, limit_messages, currency,
	to_currency, bank, to_bank, company_currency, exchange_rate, base_amount, limit, limit_action,
	approval_threshold, above_threshold, approval_required, is_manager}, plus the party's default
	`party_bank_account` / `beneficiary_iban` / `beneficiary_bank` / `beneficiary_swift` for those sent empty.
	`name`: the saved transfer itself, left out of its daily limit."""
	_check_prepare_permission()
	if transfer_type not in fees.DIRECTION:
		frappe.throw(_("Unknown transfer type {0}.").format(frappe.bold(transfer_type)))
	if method not in METHODS:
		frappe.throw(_("Unknown method {0}.").format(frappe.bold(method)))
	internal = transfer_type == lifecycle.INTERNAL
	if not internal:
		to_bank_account = None
	for account in (bank_account, to_bank_account):
		if account and frappe.db.get_value("Bank Account", account, "company") != company:
			frappe.throw(
				_("Bank Account {0} does not belong to company {1}.").format(
					frappe.bold(account), frappe.bold(company)
				)
			)
	date = getdate(execution_date or today())
	source, target = _company_bank(bank_account), _company_bank(to_bank_account)
	defaults = (
		{}
		if internal
		else _party_defaults(
			party_type, party, party_bank_account, beneficiary_iban, beneficiary_bank, beneficiary_swift
		)
	)
	transfer = frappe._dict(
		name=name if name and frappe.db.exists(DOCTYPE, name) else None,
		company=company,
		bank_account=bank_account,
		method=method,
		transfer_type=transfer_type,
		amount=flt(amount),
		currency=source.currency,
		execution_date=date,
		charge_bearer=charge_bearer,
		party_bank_account=party_bank_account or defaults.get("party_bank_account"),
		beneficiary_iban=beneficiary_iban or defaults.get("beneficiary_iban"),
		beneficiary_bank=beneficiary_bank or defaults.get("beneficiary_bank"),
		beneficiary_swift=beneficiary_swift or defaults.get("beneficiary_swift"),
		to_bank_account=to_bank_account,
	)
	scope = fees.detect_scope(transfer)
	result = fees.compute_fee(
		company,
		bank_account,
		method,
		fees.DIRECTION[transfer_type],
		scope,
		transfer.amount,
		charge_bearer if method == fees.SWIFT else None,
	)
	company_currency = erpnext.get_company_currency(company)
	rate = _exchange_rate(source.currency, company, company_currency, exchange_rate, date)
	base_amount = flt(transfer.amount * rate, frappe.get_precision(DOCTYPE, "base_amount") or 2)
	settings = transfer_settings(company)
	threshold = flt(settings.get("transfer_approval_threshold"))
	is_manager = bool(set(lifecycle.MANAGER_ROLES) & set(frappe.get_roles()))
	above_threshold = threshold > 0 and base_amount > threshold
	return {
		"scope": scope,
		"computed_fee": result.fee,
		"correspondent_fee": result.correspondent_fee,
		"fee_account": result.account,
		"fee_rule": _("Row #{0}").format(result.rule_idx) if result.rule_idx else None,
		"fee_basis": result.basis,
		"limit_messages": fees.check_limits(transfer) if bank_account and transfer.amount else [],
		"currency": source.currency,
		"bank": source.bank,
		"to_currency": target.currency,
		"to_bank": target.bank,
		"company_currency": company_currency,
		"exchange_rate": rate,
		"base_amount": base_amount,
		"limit": _limit(transfer, date),
		"limit_action": settings.get("transfer_limit_action") or "Warn",
		"approval_threshold": threshold,
		"above_threshold": above_threshold,
		"approval_required": above_threshold and not is_manager,
		"is_manager": is_manager,
		**defaults,
	}


def _company_bank(bank_account: str | None) -> frappe._dict:
	"""Bank and GL account currency of a company bank account (both None when not given)."""
	if not bank_account:
		return frappe._dict(bank=None, currency=None)
	values = frappe.db.get_value("Bank Account", bank_account, ["bank", "account"], as_dict=True) or {}
	account = values.get("account")
	return frappe._dict(bank=values.get("bank"), currency=get_account_currency(account) if account else None)


def _exchange_rate(currency, company: str, company_currency: str, exchange_rate, date) -> float:
	"""The rate the transfer will be saved with (BankTransfer.set_exchange_rate): 1 for the company currency,
	else the user's rate, else the rate of the execution date."""
	if not currency or currency == company_currency:
		return 1.0
	if flt(exchange_rate) not in (0, 1) and flt(exchange_rate) > 0:
		return flt(exchange_rate)
	return flt(posting.get_cheque_exchange_rate(currency, company, date))


def _party_defaults(
	party_type: str | None,
	party: str | None,
	party_bank_account: str | None,
	iban: str | None,
	bank: str | None,
	swift: str | None,
) -> dict:
	"""The beneficiary details the server fills on save, for the fields sent empty: the party's default bank
	account, its IBAN and bank, and the SWIFT code of the bank. Only a bank account of the party itself."""
	if not (party_type and party) or not frappe.db.exists("Party Type", party_type):
		return {}
	if not frappe.db.exists(party_type, party):
		return {}
	defaults = {}
	account_name = party_bank_account
	if not account_name:
		account_name = get_party_bank_account(party_type, party)
		if account_name:
			defaults["party_bank_account"] = account_name
	account = (
		frappe.db.get_value(
			"Bank Account",
			account_name,
			["party_type", "party", "is_company_account", "iban", "bank"],
			as_dict=True,
		)
		if account_name
		else None
	)
	own = account and not account.is_company_account
	if own and (account.party_type, account.party) == (party_type, party):
		if not iban and account.iban:
			defaults["beneficiary_iban"] = account.iban
		if not bank and account.bank:
			defaults["beneficiary_bank"] = bank = account.bank
	else:
		defaults.pop("party_bank_account", None)
	if not swift and bank:
		swift_number = frappe.db.get_value("Bank", bank, "swift_number")
		if swift_number:
			defaults["beneficiary_swift"] = swift_number
	return defaults


def _limit(transfer: frappe._dict, date) -> dict | None:
	"""The per-transaction / daily limit of the bank account and method, and the amount already used on the
	execution date by the other submitted transfers (bank account currency)."""
	if not transfer.bank_account:
		return None
	limit = fees.get_limit(transfer.company, transfer.bank_account, transfer.method)
	if not limit:
		return None
	daily = flt(limit.daily_limit)
	return {
		"method": transfer.method,
		"per_transaction_limit": flt(limit.per_transaction_limit),
		"daily_limit": daily,
		"used": fees.used_on(transfer, date) if daily > 0 else 0.0,
		"date": str(date),
	}
