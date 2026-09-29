# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Security Cheque Register (PLAN.md §7): security cheques held and given, with every filter and status.

`days_to_expiry` is shown for the cheques still held / given (negative once expired). The totals are per
direction (held from others / given to others), never added together.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from treasury.pdc.report_utils import (
	column,
	get_precision,
	report_today,
	validate_company,
	validate_date_range,
)
from treasury.security_cheques.lifecycle import INCOMING, OUTGOING
from treasury.security_cheques.report_utils import (
	CTX,
	DOCTYPE,
	AmountConverter,
	cheque_columns,
	company_currency_column,
	get_security_cheques,
	make_row,
	validate_statuses,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	validate_date_range(filters.expiry_from, filters.expiry_to)

	converter = AmountConverter(company)
	today = report_today()
	rows = [make_row(c, converter, today) for c in get_security_cheques(get_filters(filters, company))]
	return get_columns(), rows + total_rows(rows, converter.company_currency)


def get_filters(filters: frappe._dict, company: str) -> list:
	conditions = [["company", "=", company]]
	for fieldname in (
		"direction",
		"party_type",
		"party",
		"purpose",
		"amount_type",
		"reference_doctype",
		"reference_name",
		"custody_location",
		"custodian",
	):
		if filters.get(fieldname):
			conditions.append([fieldname, "=", filters.get(fieldname)])
	statuses = validate_statuses(filters.status)
	if statuses:
		conditions.append(["status", "in", statuses])
	if filters.expiry_from:
		conditions.append(["expiry_date", ">=", getdate(filters.expiry_from)])
	if filters.expiry_to:
		conditions.append(["expiry_date", "<=", getdate(filters.expiry_to)])
	return conditions


def total_rows(rows: list[frappe._dict], company_currency: str) -> list[frappe._dict]:
	"""One total per direction: security held from others and given to others are never added together."""
	totals = []
	for direction, label in (
		(INCOMING, _("Total Held from Others")),
		(OUTGOING, _("Total Given to Others")),
	):
		direction_rows = [r for r in rows if r.direction == direction]
		if direction_rows:
			totals.append(total_row(direction_rows, company_currency, label, direction))
	return totals or [total_row([], company_currency, _("Total", context=CTX))]


def total_row(rows: list[frappe._dict], company_currency: str, label: str, direction=None) -> frappe._dict:
	return frappe._dict(
		is_total=1,
		indent=0,
		direction=direction,
		cheque_no=label,
		cheque_count=len(rows),
		base_amount=flt(sum(r.base_amount for r in rows), get_precision()),
		company_currency=company_currency,
	)


def get_columns() -> list[dict]:
	return [
		*cheque_columns(),
		column("cheque_count", _("Cheques"), "Int", 70),
		column("required_amount", _("Required Amount"), "Currency", 120, options="currency"),
		column("issue_date", _("Issue Date"), "Date", 100),
		column("expiry_date", _("Expiry Date"), "Date", 100),
		column("days_to_expiry", _("Days to Expiry"), "Int", 90),
		column("status", _("Status"), width=100),
		column("custody_location", _("Custody Location"), "Link", 140, options="Custody Location"),
		column("custodian", _("Custodian"), "Link", 140, options="User"),
		column("reference_doctype", _("Reference Type"), "Link", 120, options="DocType"),
		column("reference_name", _("Reference Name"), "Dynamic Link", 150, options="reference_doctype"),
		column("converted_pdc_cheque", _("Converted PDC Cheque"), "Link", 150, options="PDC Cheque"),
		company_currency_column(),
	]
