"""Treasury buttons of a Custody Request (spec 9 §3.5): read-only, like the mappers of `treasury.pdc.api`.

They return a new, unsaved document that the client opens with `frappe.model.sync` + `set_route`; nothing is
written until the user saves it through the normal insert path, where `documents.validate` checks it again.
"""

import erpnext
import frappe
from erpnext.accounts.doctype.bank_account.bank_account import get_party_bank_account
from frappe import _
from frappe.utils import today

from treasury.accounting import payment_modes, posting
from treasury.bank_transfers import api as transfer_api
from treasury.bank_transfers import fees
from treasury.integrations.custody import PARTY_TYPE, REQUEST, compat, documents, is_installed
from treasury.pdc.doctype.pdc_cheque.pdc_cheque import NAMING_SERIES as CHEQUE_NAMING_SERIES
from treasury.treasury_setup.doctype.treasury_company_settings.treasury_company_settings import (
	TRANSFER_DEFAULT_BANK_FIELDS,
)
from treasury.utils import source_documents as sd
from treasury.utils.party import can_read_party, get_party_name

CHEQUE = "PDC Cheque"
TRANSFER = "Bank Transfer"
OUTGOING, INCOMING = documents.OUTGOING, documents.INCOMING
TREASURY_ROLES = ("Treasury User", "Treasury Manager", "System Manager")
DEFAULT_METHOD = "Cheque"
# Treasury Company Settings `custody_default_method` -> Bank Transfer method (SWIFT is never offered)
TRANSFER_METHODS = {"Bank Transfer": "Normal", "Fawran": "Fawran"}
TRANSFER_METHOD_CHOICES = ("Normal", "Fawran")
# open cheques / transfers listed on the request (the rest is counted)
OPEN_DOCUMENTS_SHOWN = 3


@frappe.whitelist(methods=["GET"])
def get_payment_context(custody_request: str) -> dict:
	"""What the Treasury buttons of the request may offer: {pay: {amount, purpose, reason, blocked,
	draft_payment_entry}, receive: {...}, can_cheque, can_transfer, default_method, currency, custodian_name,
	transfer_banks: {Normal, Fawran} (the banks the transfer mapper would pick), open_documents (the open
	cheques / transfers of the request the user may read, at most 3), more_documents}. Amounts are net of the
	open cheques / transfers. Without a Treasury role or the create permission of a cheque / transfer: only
	{can_cheque: False, can_transfer: False} (no Treasury details)."""
	with sd.treasury_refusals():
		_require_installed()
		req = _get_request(custody_request)
		has_role = _has_treasury_role()
		can_cheque = bool(has_role and frappe.has_permission(CHEQUE, "create"))
		can_transfer = bool(has_role and frappe.has_permission(TRANSFER, "create"))
		if not (can_cheque or can_transfer):
			return {"can_cheque": False, "can_transfer": False}
		pay = _side(req, OUTGOING)
		receive = _side(req, INCOMING)
		open_docs, more = documents.open_documents(req.name, OPEN_DOCUMENTS_SHOWN)
		return {
			"pay": pay,
			"receive": receive,
			"can_cheque": can_cheque,
			"can_transfer": can_transfer,
			"default_method": _settings_value(req.company, "custody_default_method") or DEFAULT_METHOD,
			"currency": erpnext.get_company_currency(req.company),
			"custodian_name": req.get("custodian_name") or req.custodian,
			"transfer_banks": _transfer_banks(req.company) if can_transfer and pay["amount"] > 0 else {},
			"open_documents": open_docs,
			"more_documents": more,
		}


@frappe.whitelist(methods=["GET"])
def make_pdc_cheque(custody_request: str, direction: str) -> dict:
	"""A new PDC Cheque (not inserted) paying (Outgoing) or receiving back (Incoming) what the request still
	accepts: the custodian, the request's accounting details and its "Created From" link."""
	with sd.treasury_refusals():
		return _make_pdc_cheque(custody_request, direction)


def _make_pdc_cheque(custody_request: str, direction: str) -> dict:
	_require_installed()
	if direction not in (OUTGOING, INCOMING):
		frappe.throw(_("Direction must be Outgoing or Incoming."))
	req = _get_request(custody_request)
	sd.check_create_permission(CHEQUE, req.company)
	_require_treasury_role()
	posting.get_company_settings(req.company)
	a = documents.check_request(req, direction)

	doc = frappe.new_doc(CHEQUE)
	doc.naming_series = CHEQUE_NAMING_SERIES[direction]
	doc.company = req.company
	doc.direction = direction
	doc.party_type = PARTY_TYPE
	doc.party = req.custodian
	doc.party_name = req.get("custodian_name") or get_party_name(PARTY_TYPE, req.custodian)
	doc.issue_date = today()
	doc.original_due_date = today()
	doc.bank_account = _cheque_bank(req.company, direction)
	doc.currency = erpnext.get_company_currency(req.company)
	doc.exchange_rate = 1
	if doc.bank_account:
		method = payment_modes.CHEQUE_METHOD[direction]
		doc.mode_of_payment = payment_modes.resolve(req.company, doc.bank_account, method).mode_of_payment
	if direction == OUTGOING:
		doc.cheque_book = _cheque_book(req.company, doc.bank_account)
	doc.cheque_no = None
	doc.amount = a.amount
	label = _("Custody payout of {0}") if direction == OUTGOING else _("Custody returned on {0}")
	doc.remarks = label.format(req.name)
	doc.source_doctype, doc.source_name = REQUEST, req.name
	sd.copy_accounting(req, doc)
	if can_read_party(PARTY_TYPE, req.custodian):
		doc.party_bank_account = get_party_bank_account(PARTY_TYPE, req.custodian)
	return doc.as_dict()


@frappe.whitelist(methods=["GET"])
def make_bank_transfer(custody_request: str, method: str | None = None) -> dict:
	"""A new Bank Transfer (not inserted, Party Payment, Normal or Fawran) paying the custodian what the request
	still accepts; the fee is posted on its own voucher when the transfer is sent (custody refuses deductions)."""
	with sd.treasury_refusals():
		return _make_bank_transfer(custody_request, method)


def _make_bank_transfer(custody_request: str, method: str | None) -> dict:
	_require_installed()
	if method and method not in TRANSFER_METHOD_CHOICES:
		frappe.throw(_("Unknown method {0}.").format(frappe.bold(method)))
	req = _get_request(custody_request)
	sd.check_create_permission(TRANSFER, req.company)
	transfer_api.check_company_permission(req.company)
	_require_treasury_role()
	settings = posting.get_company_settings(req.company)
	a = documents.check_request(req, OUTGOING)

	doc = frappe.new_doc(TRANSFER)
	doc.company = req.company
	doc.transfer_type = documents.PARTY_PAYMENT
	doc.method = method or _default_transfer_method(settings)
	doc.bank_account = _transfer_bank(req.company, doc.method, settings)
	doc.charge_bearer = settings.default_charge_bearer or fees.SHA
	doc.request_date = doc.execution_date = today()
	doc.currency = erpnext.get_company_currency(req.company)
	doc.exchange_rate = 1
	doc.party_type = PARTY_TYPE
	doc.party = req.custodian
	doc.party_name = req.get("custodian_name") or get_party_name(PARTY_TYPE, req.custodian)
	if can_read_party(PARTY_TYPE, req.custodian):
		doc.party_bank_account = get_party_bank_account(PARTY_TYPE, req.custodian)
	doc.beneficiary_iban = _employee_iban(req.custodian)
	sd.copy_accounting(req, doc)
	doc.amount = a.amount
	doc.remarks = _("Custody payout of {0}").format(req.name)
	doc.source_doctype, doc.source_name = REQUEST, req.name
	return doc.as_dict()


# ---------------------------------------------------------------------------


def _require_installed():
	if not is_installed():
		frappe.throw(_("The Custody app is not installed."))


def _get_request(name: str):
	"""The request the user may read; one error whether it is missing or not permitted."""
	req = documents.get_request(name)
	if not req or not frappe.has_permission(REQUEST, "read", req):
		documents.request_not_permitted(name)
	return req


def _has_treasury_role() -> bool:
	return bool(set(TREASURY_ROLES) & set(frappe.get_roles()))


def _require_treasury_role():
	if not _has_treasury_role():
		frappe.throw(
			_("This action requires one of the roles: {0}.").format(", ".join(_(r) for r in TREASURY_ROLES)),
			frappe.PermissionError,
			title=_("Not Permitted"),
		)


def _side(req, direction: str) -> dict:
	"""{amount, purpose, reason, blocked} of one direction; amount 0 with the reason when nothing can be made
	now. `blocked`: custody would still accept a payment, but a draft replenishment or the open cheques /
	transfers stand in the way (the form says so)."""
	a, reason = documents.refusal(req, direction)
	draft = None
	if reason and a and documents.draft_refusal(req, direction, a):
		# the draft replenishment Payment Entry (F13): the form links it
		draft = next(iter(compat.draft_replenishments(req.name)), None)
	return {
		"amount": 0 if reason else a.amount,
		"purpose": a.purpose if a else None,
		"reason": reason,
		"blocked": bool(reason and a and a.allowance > 0 and (draft or a.amount <= 0)),
		"draft_payment_entry": draft,
	}


def _settings_value(company: str, fieldname: str):
	"""A Treasury Company Settings value, None when the settings or the field (a later release) are missing."""
	if not frappe.db.exists("Treasury Company Settings", company):
		return None
	if not frappe.get_meta("Treasury Company Settings").has_field(fieldname):
		return None
	return frappe.db.get_value("Treasury Company Settings", company, fieldname)


def _in_company_currency(bank_account: str | None, company: str) -> bool:
	return bool(bank_account) and sd.bank_currency(bank_account) == erpnext.get_company_currency(company)


def _cheque_bank(company: str, direction: str) -> str | None:
	"""Outgoing: the custody bank of the settings, else the default outgoing cheque bank, else the first mapped
	bank, in the company currency. Incoming: the default incoming cheque bank when in the company currency."""
	company_currency = erpnext.get_company_currency(company)
	if direction == INCOMING:
		bank = _settings_value(company, sd.DEFAULT_CHEQUE_BANK_FIELDS[INCOMING])
		return bank if _in_company_currency(bank, company) else None
	candidates = [
		_settings_value(company, "custody_bank_account"),
		sd.default_cheque_bank(company, OUTGOING, company_currency),
	]
	settings = posting.get_company_settings(company)
	candidates += [row.bank_account for row in sorted(settings.get("pdc_accounts"), key=lambda r: r.idx)]
	return next((bank for bank in candidates if _in_company_currency(bank, company)), None)


def _default_transfer_method(settings) -> str:
	"""Normal or Fawran: the custody payout method of the settings, else the default transfer method (SWIFT is
	never offered to a custodian)."""
	method = TRANSFER_METHODS.get(settings.get("custody_default_method"))
	if not method and settings.get("default_transfer_method") in TRANSFER_METHOD_CHOICES:
		method = settings.default_transfer_method
	return method or "Normal"


def _transfer_bank(company: str, method: str, settings=None) -> str | None:
	"""The custody bank of the settings, else the default bank of the method, else the default outgoing cheque
	bank; only a bank in the company currency (custody is paid in it)."""
	settings = settings or posting.get_company_settings(company)
	candidates = (
		settings.get("custody_bank_account"),
		settings.get(TRANSFER_DEFAULT_BANK_FIELDS[method]),
		transfer_api._fallback_bank(company, documents.PARTY_PAYMENT),
	)
	return next((bank for bank in candidates if _in_company_currency(bank, company)), None)


def _transfer_banks(company: str) -> dict:
	"""{method: bank} the transfer mapper would choose, for the fee preview of the request's dialog."""
	if not frappe.db.exists("Treasury Company Settings", company):
		return {}
	settings = posting.get_company_settings(company)
	return {method: _transfer_bank(company, method, settings) for method in TRANSFER_METHOD_CHOICES}


def _employee_iban(custodian: str) -> str | None:
	"""The IBAN of the custodian's Employee, when Employee has that field and the user may read the Employee."""
	employee = frappe.db.get_value(PARTY_TYPE, custodian, "employee")
	if not employee or not frappe.get_meta("Employee").has_field("iban"):
		return None
	if not can_read_party("Employee", employee):
		return None
	return frappe.db.get_value("Employee", employee, "iban") or None


def _cheque_book(company: str, bank_account: str | None) -> str | None:
	"""The custody cheque book of the settings when it is active, of the chosen bank and has a free leaf; else
	the default cheque book of the bank."""
	if not bank_account:
		return None
	book = _settings_value(company, "custody_cheque_book")
	if book:
		from treasury.pdc import batch

		values = frappe.db.get_value("Cheque Book", book, ["status", "bank_account"], as_dict=True)
		if values and values.status == "Active" and values.bank_account == bank_account:
			if batch.preview_leaves(book, 1):
				return book
	return sd.default_cheque_book(company, bank_account)
