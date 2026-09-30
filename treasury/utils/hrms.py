"""HRMS documents Treasury may pay (Expense Claim, Employee Advance), without importing hrms.

hrms is optional: every entry point first checks `hrms_installed()`. Whether a Payment Entry accepts these
documents as references depends on the Payment Entry class the site runs (hrms 16 replaces it by
`EmployeePaymentEntry`, whose `get_valid_reference_doctypes` adds them for an Employee): Treasury asks the class
itself (`employee_reference_doctypes`) instead of assuming a version.

Field names verified in hrms 16.20 (`hr/doctype/expense_claim`, `hr/doctype/employee_advance`,
`overrides/employee_payment_entry.py`).
"""

import frappe
from frappe import _
from frappe.utils import flt

HRMS_DOCTYPES = ("Expense Claim", "Employee Advance")
# Fields read by `outstanding` (and by the mappers' account check).
EXPENSE_CLAIM_FIELDS = (
	"total_sanctioned_amount",
	"total_taxes_and_charges",
	"total_amount_reimbursed",
	"total_advance_amount",
)
EMPLOYEE_ADVANCE_FIELDS = ("advance_amount", "paid_amount")
# The account each document is booked on (compared with the employee's default party account).
PARTY_ACCOUNT_FIELD = {"Expense Claim": "payable_account", "Employee Advance": "advance_account"}


def hrms_installed() -> bool:
	return "hrms" in frappe.get_installed_apps() and bool(frappe.db.exists("DocType", "Expense Claim"))


def employee_reference_doctypes() -> tuple[str, ...]:
	"""Reference doctypes the active Payment Entry class accepts for an Employee (cached per request)."""
	cached = getattr(frappe.local, "treasury_employee_reference_doctypes", None)
	if cached is not None:
		return cached
	pe = frappe.new_doc("Payment Entry")
	pe.party_type = "Employee"
	getter = getattr(pe, "get_valid_reference_doctypes", None)
	result = tuple(getter() or ()) if getter else ("Journal Entry",)
	frappe.local.treasury_employee_reference_doctypes = result
	return result


def supported_reference_doctypes() -> tuple[str, ...]:
	"""The HRMS doctypes Treasury may allocate a payment to on this site (none without hrms)."""
	if not hrms_installed():
		return ()
	accepted = employee_reference_doctypes()
	return tuple(d for d in HRMS_DOCTYPES if d in accepted)


def check_supported(doctype: str):
	"""An unallocated payment would leave the claim / advance unpaid: no silent on-account fallback."""
	if not hrms_installed():
		frappe.throw(_("{0} needs the HRMS app.").format(_(doctype)))
	if doctype not in employee_reference_doctypes():
		frappe.throw(
			_(
				"This HRMS version does not accept {0} as a Payment Entry reference; pay it from HRMS."
			).format(_(doctype))
		)


def outstanding(doctype: str, name: str, party_currency: str | None = None) -> float:
	"""What is still to be paid on an Expense Claim / Employee Advance, in its own currency, the way hrms
	computes it for a Payment Entry reference (`get_outstanding_amount_for_claim`, advance - paid)."""
	fields = EXPENSE_CLAIM_FIELDS if doctype == "Expense Claim" else EMPLOYEE_ADVANCE_FIELDS
	meta = frappe.get_meta(doctype)
	if doctype not in HRMS_DOCTYPES or not all(meta.has_field(f) for f in fields):
		frappe.throw(_("{0} cannot be read with this HRMS version.").format(_(doctype)))
	values = frappe.db.get_value(doctype, name, [*fields, "docstatus"], as_dict=True)
	if not values or values.docstatus != 1:
		return 0
	if doctype == "Expense Claim":
		amount = (
			flt(values.total_sanctioned_amount)
			+ flt(values.total_taxes_and_charges)
			- flt(values.total_amount_reimbursed)
			- flt(values.total_advance_amount)
		)
	else:
		currency = frappe.db.get_value(doctype, name, "currency") if meta.has_field("currency") else None
		if party_currency and currency and currency != party_currency:
			# hrms converts with an exchange rate this version does not store: settle it from HRMS
			frappe.throw(_("{0} cannot be read with this HRMS version.").format(_(doctype)))
		amount = flt(values.advance_amount) - flt(values.paid_amount)
	return max(flt(amount, frappe.get_precision(doctype, fields[0]) or 2), 0)
