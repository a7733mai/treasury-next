"""Shared helpers of the Security Cheque reports and number cards (read only).

Security cheques are always read with `frappe.get_list`, so the user's permissions and User Permissions
apply; the Company filter is checked like the PDC reports do (treasury.pdc.report_utils.validate_company).
Amounts are summed in the company currency with the PDC reports' converter (today's rate for a foreign
currency cheque); an Open / Blank cheque carries no amount and is counted, never summed.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate

from treasury.pdc.report_utils import AmountConverter, as_list, column, get_precision
from treasury.security_cheques.lifecycle import ALL_STATUSES, FINAL_STATUSES, OPEN

DOCTYPE = "Security Cheque"
# Translation context of the generic words of these reports (see locale/ar.po)
CTX = "Treasury Report"

CHEQUE_FIELDS = [
	"name",
	"direction",
	"company",
	"party_type",
	"party",
	"party_name",
	"purpose",
	"cheque_no",
	"drawer_bank",
	"bank_account",
	"amount_type",
	"amount",
	"currency",
	"required_amount",
	"issue_date",
	"expiry_date",
	"status",
	"custody_location",
	"custodian",
	"reference_doctype",
	"reference_name",
	"converted_pdc_cheque",
]


def get_security_cheques(filters, fields=None, order_by: str = "expiry_date asc, name asc"):
	"""Security Cheques the user may read (`frappe.get_list`: permissions and User Permissions apply)."""
	return frappe.get_list(DOCTYPE, filters=filters, fields=fields or CHEQUE_FIELDS, order_by=order_by)


def validate_statuses(value) -> list[str]:
	statuses = as_list(value)
	if invalid := set(statuses) - set(ALL_STATUSES):
		frappe.throw(_("Invalid Status: {0}").format(", ".join(sorted(invalid))))
	return statuses


def is_open(cheque) -> bool:
	return cheque.amount_type == OPEN


def days_to_expiry(cheque, today) -> int | None:
	"""Days from `today` to the expiry date of a cheque still held / given (negative: expired)."""
	if not cheque.expiry_date or cheque.status in FINAL_STATUSES:
		return None
	return date_diff(getdate(cheque.expiry_date), today)


def make_row(cheque, converter: AmountConverter, today) -> frappe._dict:
	"""A report row of one security cheque (amounts in its currency and in the company currency)."""
	precision = get_precision()
	return frappe._dict(
		security_cheque=cheque.name,
		direction=cheque.direction,
		party_type=cheque.party_type,
		party=cheque.party,
		party_name=cheque.party_name,
		purpose=cheque.purpose,
		cheque_no=cheque.cheque_no,
		bank=cheque.bank_account or cheque.drawer_bank,
		amount_type=cheque.amount_type,
		amount=flt(cheque.amount, precision),
		currency=cheque.currency,
		base_amount=0.0 if is_open(cheque) else converter.company_amount(cheque),
		required_amount=flt(cheque.required_amount, precision),
		company_currency=converter.company_currency,
		issue_date=cheque.issue_date,
		expiry_date=cheque.expiry_date,
		days_to_expiry=days_to_expiry(cheque, today),
		status=cheque.status,
		custody_location=cheque.custody_location,
		custodian=cheque.custodian,
		reference_doctype=cheque.reference_doctype,
		reference_name=cheque.reference_name,
		converted_pdc_cheque=cheque.converted_pdc_cheque,
		indent=0,
	)


def base_required(cheque, converter: AmountConverter) -> float:
	"""The required amount of a cheque in the company currency (same rate as its amount)."""
	return flt(flt(cheque.required_amount) * converter.rate(cheque), converter.precision)


def cheque_columns() -> list[dict]:
	"""Columns identifying a security cheque, shared by the list-style reports."""
	return [
		column("security_cheque", _("Security Cheque"), "Link", 160, options=DOCTYPE),
		column("direction", _("Direction"), width=90),
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 150, options="party_type"),
		column("party_name", _("Party Name"), width=160),
		column("purpose", _("Purpose", context="Security Cheque"), width=130),
		column("cheque_no", _("Cheque No")),
		column("amount_type", _("Amount Type"), width=100),
		column("amount", _("Amount"), "Currency", 120, options="currency"),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		column("base_amount", _("Amount (Company Currency)"), "Currency", 140, options="company_currency"),
	]


def company_currency_column() -> dict:
	return column(
		"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
	)
