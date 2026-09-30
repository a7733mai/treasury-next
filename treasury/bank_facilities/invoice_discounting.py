"""ERPNext Invoice Discounting under a Bank Facility (spec 7g §3.10, F8).

ERPNext's Invoice Discounting moves the discounted receivables and books its own loan (disbursement / close
Journal Entries); Treasury posts nothing. The custom field `treasury_facility` (treasury.install) links it to a
facility with an "Invoice Discounting" sub-limit: the total amount is checked against the available sub-limit
on submit (facility locked, Active on the loan start date) and counts against it while the discounting is
Sanctioned or Disbursed (`get_invoice_discounting_utilisation`, hook `treasury_facility_utilisation_sources`).
Historical utilisation uses the current status (documented in the README). The facility's stored utilisation is
refreshed on the discounting's submit / cancel and on its loan Journal Entries (status Disbursed / Settled).
"""

import erpnext
import frappe
from frappe import _
from frappe.utils import flt, getdate

from treasury.bank_facilities import instruments
from treasury.bank_facilities.utilisation import refresh_utilisation

DOCTYPE = "Invoice Discounting"
LIMIT_TYPE = "Invoice Discounting"
OPEN_STATUSES = ("Sanctioned", "Disbursed")
# its own ledger rows (kept, marked cancelled, and reversed by ERPNext on cancel)
LEDGER_DOCTYPES = ("GL Entry", "Payment Ledger Entry")


def validate(doc, method=None):
	"""doc_events: the facility is of the company, in the company currency, with an Invoice Discounting
	sub-limit."""
	if not doc.get("treasury_facility"):
		return
	facility = frappe.db.get_value(
		"Bank Facility", doc.treasury_facility, ["company", "currency"], as_dict=True
	)
	if not facility or facility.company != doc.company:
		frappe.throw(
			_("Bank Facility {0} does not belong to company {1}.").format(
				frappe.bold(doc.treasury_facility), frappe.bold(doc.company)
			)
		)
	company_currency = erpnext.get_company_currency(doc.company)
	if facility.currency != company_currency:
		frappe.throw(
			_("Bank Facility {0} must be in the company currency {1}.").format(
				frappe.bold(doc.treasury_facility), company_currency
			)
		)
	if not frappe.db.exists(
		"Bank Facility Limit",
		{"parenttype": "Bank Facility", "parent": doc.treasury_facility, "limit_type": LIMIT_TYPE},
	):
		frappe.throw(
			_("Bank Facility {0} has no Invoice Discounting sub-limit.").format(
				frappe.bold(doc.treasury_facility)
			)
		)


def before_submit(doc, method=None):
	"""doc_events: the total amount fits the available sub-limit (and total limit) of the Active facility."""
	if not doc.get("treasury_facility"):
		return
	facility = instruments.lock_facility(doc.treasury_facility)
	instruments.validate_facility_open(facility, doc.loan_start_date or doc.posting_date, _(LIMIT_TYPE))
	currency = erpnext.get_company_currency(doc.company)
	instruments.check_available(facility, LIMIT_TYPE, flt(doc.total_amount), currency, _(LIMIT_TYPE))


def before_cancel(doc, method=None):
	"""doc_events: ERPNext's Invoice Discounting does not list its own ledger entries among the documents ignored
	by Frappe's back-link check on cancel (unlike its other accounting documents), so its cancellation always
	fails with "is linked with GL Entry". For a discounting under a facility (whose cancellation frees the
	sub-limit) they are ignored, as ERPNext does for its vouchers; a submitted loan Journal Entry referencing it
	still blocks the cancellation."""
	if not doc.get("treasury_facility"):
		return
	current = doc.get("ignore_linked_doctypes") or ()
	if isinstance(current, str):
		current = (current,)
	doc.ignore_linked_doctypes = (*current, *LEDGER_DOCTYPES)


def refresh(doc, method=None):
	"""doc_events on_submit / on_cancel: the facility's display utilisation."""
	if doc.get("treasury_facility"):
		refresh_utilisation(doc.treasury_facility)


def on_journal_entry(doc, method=None):
	"""doc_events of Journal Entry on_submit / on_cancel: ERPNext moves an Invoice Discounting to Disbursed /
	Settled (or back) from its loan Journal Entries, which changes what counts against the facility."""
	names = {
		row.reference_name
		for row in doc.get("accounts") or []
		if row.reference_type == DOCTYPE and row.reference_name
	}
	if not names or not frappe.get_meta(DOCTYPE).has_field("treasury_facility"):
		return
	for facility in sorted(
		set(frappe.get_all(DOCTYPE, filters={"name": ["in", list(names)]}, pluck="treasury_facility"))
		- {None, ""}
	):
		refresh_utilisation(facility)


def get_invoice_discounting_utilisation(facility, as_on=None) -> list[dict]:
	"""Hook `treasury_facility_utilisation_sources`: the total amount of the facility's submitted, Sanctioned /
	Disbursed Invoice Discountings (on `as_on`: those whose loan started by then)."""
	if not frappe.get_meta(DOCTYPE).has_field("treasury_facility"):
		return []
	filters = {"treasury_facility": facility.name, "docstatus": 1, "status": ["in", OPEN_STATUSES]}
	rows = frappe.get_all(
		DOCTYPE, filters=filters, fields=["total_amount", "loan_start_date", "posting_date"]
	)
	amount = sum(
		flt(r.total_amount)
		for r in rows
		if not as_on or getdate(r.loan_start_date or r.posting_date) <= getdate(as_on)
	)
	return [{"limit_type": LIMIT_TYPE, "amount": amount}] if amount else []
