"""Security Cheque mapper for the Treasury buttons of quotations, orders and party masters (spec 7c §3.4).

Read only (like Frappe's `make_mapped_doc`): it returns a new, unsaved Security Cheque (`__islocal`); the leaf
of an outgoing cheque book is allocated only when the user saves it. A security cheque never posts anything
(hard rule 4), so draft quotations are accepted: a tender bond goes with the bid.
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import flt, getdate, today

from treasury.accounting import posting
from treasury.security_cheques import lifecycle
from treasury.security_cheques.doctype.security_cheque.security_cheque import NAMING_SERIES
from treasury.utils import source_documents as sd

DOCTYPE = "Security Cheque"
TENDER = "Tender"
# source doctype -> (direction, purpose, party type, party field); Quotation: see `_party`
SOURCES = {
	"Quotation": (lifecycle.OUTGOING, TENDER, "Customer", "party_name"),
	"Supplier Quotation": (lifecycle.INCOMING, TENDER, "Supplier", "supplier"),
	"Sales Order": (lifecycle.OUTGOING, "Contract Performance", "Customer", "customer"),
	"Purchase Order": (lifecycle.INCOMING, "Contract Performance", "Supplier", "supplier"),
	"Customer": (lifecycle.INCOMING, "Other", "Customer", None),
	"Supplier": (lifecycle.INCOMING, "Other", "Supplier", None),
	"Employee": (lifecycle.INCOMING, "Employee Custody", "Employee", None),
	# spec 7g §3.11: our cheque to the lender as facility collateral
	"Bank Facility": (lifecycle.OUTGOING, "Facility Collateral", "Supplier", "lender"),
}
FACILITY = "Bank Facility"
QUOTATIONS = ("Quotation", "Supplier Quotation")
# The bond kind and its Treasury Company Settings % of the source's grand total: tender bonds go with a bid
# (quotations), performance bonds with a contract (orders).
TENDER_BOND, PERFORMANCE_BOND = "tender", "performance"
BOND_PERCENT_FIELDS = {TENDER_BOND: "tender_bond_percent", PERFORMANCE_BOND: "performance_bond_percent"}


@frappe.whitelist()
def make_security_cheque(source_doctype: str, source_name: str, company: str | None = None) -> dict:
	"""A new Security Cheque (not inserted) guaranteeing a quotation / order, or held for a party master.
	`company`: for a party master (default: the user's default company); a document's own company wins.

	When the amount is the Tender Bond % (quotations) or Performance Bond % (orders) of the source's grand total,
	the result also carries `__bond` = {kind, percent, base, currency}: the form explains the amount with it
	(client only)."""
	with sd.treasury_refusals():
		return _make_security_cheque(source_doctype, source_name, company)


def _make_security_cheque(source_doctype: str, source_name: str, company: str | None) -> dict:
	frappe.has_permission(DOCTYPE, "create", throw=True)
	if source_doctype not in SOURCES:
		frappe.throw(_("{0} cannot be created from {1}.").format(_(DOCTYPE), _(source_doctype or "-")))
	if not source_name or not frappe.db.exists(source_doctype, source_name):
		frappe.throw(_("{0} {1} does not exist").format(_(source_doctype), source_name or "-"))
	source = frappe.get_doc(source_doctype, source_name)
	source.check_permission("read")
	if source_doctype in sd.PARTY_MASTERS:
		sd.check_active_party(source)
	company = source.get("company") or sd.default_company(company)
	sd.check_create_permission(DOCTYPE, company)
	if source.docstatus == 2:
		frappe.throw(_("{0} {1} is cancelled.").format(_(source_doctype), frappe.bold(source_name)))

	direction, purpose, _party_type, _party_field = SOURCES[source_doctype]
	company_currency = erpnext.get_company_currency(company)
	has_settings = bool(frappe.db.exists("Treasury Company Settings", company))
	settings = posting.get_company_settings(company) if has_settings else frappe._dict()
	doc = frappe.new_doc(DOCTYPE)
	doc.naming_series = NAMING_SERIES[direction]
	doc.company = company
	doc.direction = direction
	doc.purpose = purpose
	_set_party(doc, source)
	doc.amount_type = lifecycle.FIXED
	doc.issue_date = doc.cheque_date = today()
	valid_till = source.get("valid_till") if source_doctype in QUOTATIONS else None
	if valid_till and getdate(valid_till) >= getdate(today()):
		doc.expiry_date = getdate(valid_till)

	if direction == lifecycle.OUTGOING:
		if has_settings:
			doc.bank_account = _facility_bank(source) or sd.default_cheque_bank(
				company, direction, company_currency
			)
			doc.cheque_book = sd.default_cheque_book(company, doc.bank_account)
		doc.currency = sd.bank_currency(doc.bank_account) or company_currency
	else:
		doc.currency = source.get("currency") if source.meta.has_field("grand_total") else None
		doc.currency = doc.currency or company_currency

	bond = None
	if source_doctype == FACILITY:
		_set_facility_values(doc, source)
	elif source_doctype not in sd.PARTY_MASTERS:
		doc.reference_doctype, doc.reference_name = source_doctype, source_name
		kind = TENDER_BOND if source_doctype in QUOTATIONS else PERFORMANCE_BOND
		bond = _set_amount(doc, source, kind, flt(settings.get(BOND_PERCENT_FIELDS[kind])), company_currency)
		doc.remarks = _remarks(kind, source_doctype, source_name, bond)
	values = doc.as_dict()
	if bond:
		values["__bond"] = bond
	return values


def _remarks(kind: str, source_doctype: str, source_name: str, bond: dict | None) -> str:
	if kind == TENDER_BOND:
		return _("Tender bond for {0} {1}").format(_(source_doctype), source_name)
	if bond:
		return _("Performance bond {0}% for {1} {2}").format(
			f"{flt(bond['percent']):g}", _(source_doctype), source_name
		)
	return _("Performance bond for {0} {1}").format(_(source_doctype), source_name)


def _facility_bank(source) -> str | None:
	"""A facility's collateral cheque is drawn on its Default Bank Account when that is in its currency."""
	if source.doctype != FACILITY or not source.get("default_bank_account"):
		return None
	if sd.bank_currency(source.default_bank_account) != source.currency:
		return None
	return source.default_bank_account


def _set_facility_values(doc, source):
	"""Facility collateral (spec 7g §3.11): the whole limit (in the facility currency only), until the facility
	expires, referencing the facility."""
	if source.status in ("Draft", "Closed"):
		frappe.throw(
			_("{0} {1} is {2}.").format(
				_(FACILITY), frappe.bold(source.name), _(source.status, context=FACILITY)
			)
		)
	if doc.currency == source.currency:
		doc.amount = doc.required_amount = flt(source.total_limit)
	doc.expiry_date = getdate(source.expiry_date)
	doc.reference_doctype, doc.reference_name = FACILITY, source.name
	doc.remarks = _("Security cheque for Bank Facility {0}").format(source.name)


def _set_party(doc, source):
	"""The party of the source; a quotation to a Lead / Prospect: party "Other" with the customer name; a
	facility without a lender Supplier yet: party "Other" with the lender's name."""
	_direction, _purpose, party_type, party_field = SOURCES[source.doctype]
	if source.doctype == "Quotation" and source.get("quotation_to") != "Customer":
		doc.party_type = lifecycle.OTHER
		doc.party_name_other = source.get("customer_name") or source.get("party_name")
		return
	if source.doctype == FACILITY and not source.get("lender"):
		doc.party_type = lifecycle.OTHER
		doc.party_name_other = source.get("lender_name") or source.get("bank")
		return
	doc.party_type = party_type
	doc.party = source.get(party_field) if party_field else source.name


def _set_amount(doc, source, kind: str, percent: float, company_currency: str) -> dict | None:
	"""`percent` of the source's grand total, in the cheque currency (none when it cannot be expressed).
	Returns {kind, percent, base, currency} when the amount was set."""
	if percent <= 0 or not source.meta.has_field("grand_total"):
		return None
	if doc.currency == source.get("currency"):
		base = flt(source.grand_total)
	elif doc.currency == company_currency:
		base = flt(source.get("base_grand_total"))
	else:
		return None
	amount = flt(base * percent / 100, frappe.get_precision(DOCTYPE, "amount") or 2)
	if amount <= 0:
		return None
	doc.amount = doc.required_amount = amount
	return {"kind": kind, "percent": percent, "base": base, "currency": doc.currency}
