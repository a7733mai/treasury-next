"""Bank Transfer helpers for the forms (read only: they never write).

- `make_bank_transfer`: a new, unsaved Bank Transfer for an invoice, order or party (the buttons on the ERPNext
  documents come in a later phase); the client opens it with `frappe.model.sync` + `set_route`.
- `get_fee_preview`: the scope, the fee of the matching rule and the limit messages of a transfer being
  entered, for the fee preview of the form.
"""

import erpnext
import frappe
from erpnext.accounts.doctype.account.account import get_account_currency
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.contacts.doctype.contact.contact import get_default_contact
from frappe.utils import flt, today

from treasury.accounting import dimensions, posting
from treasury.bank_transfers import fees, lifecycle
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
) -> dict:
	"""{scope, computed_fee, correspondent_fee, fee_account, fee_rule, fee_basis, limit_messages}."""
	if not (frappe.has_permission(DOCTYPE, "create") or frappe.has_permission(DOCTYPE, "write")):
		frappe.throw(_("You are not permitted to prepare Bank Transfers."), frappe.PermissionError)
	if transfer_type not in fees.DIRECTION:
		frappe.throw(_("Unknown transfer type {0}.").format(frappe.bold(transfer_type)))
	if method not in METHODS:
		frappe.throw(_("Unknown method {0}.").format(frappe.bold(method)))
	for account in (bank_account, to_bank_account):
		if account and frappe.db.get_value("Bank Account", account, "company") != company:
			frappe.throw(
				_("Bank Account {0} does not belong to company {1}.").format(
					frappe.bold(account), frappe.bold(company)
				)
			)
	gl_account = frappe.db.get_value("Bank Account", bank_account, "account") if bank_account else None
	transfer = frappe._dict(
		name=None,
		company=company,
		bank_account=bank_account,
		method=method,
		transfer_type=transfer_type,
		amount=flt(amount),
		currency=get_account_currency(gl_account) if gl_account else None,
		execution_date=execution_date or today(),
		charge_bearer=charge_bearer,
		party_bank_account=party_bank_account,
		beneficiary_iban=beneficiary_iban,
		beneficiary_bank=beneficiary_bank,
		beneficiary_swift=beneficiary_swift,
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
	return {
		"scope": scope,
		"computed_fee": result.fee,
		"correspondent_fee": result.correspondent_fee,
		"fee_account": result.account,
		"fee_rule": _("Row #{0}").format(result.rule_idx) if result.rule_idx else None,
		"fee_basis": result.basis,
		"limit_messages": fees.check_limits(transfer) if bank_account and transfer.amount else [],
	}
