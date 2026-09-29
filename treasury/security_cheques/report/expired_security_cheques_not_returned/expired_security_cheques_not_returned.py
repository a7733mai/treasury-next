# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Expired Security Cheques Not Returned (PLAN.md §7): cheques whose guarantee ended before As On but that
are still held (Received / In Custody) or given (Issued) — to be returned, or recovered from the party.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate

from treasury.pdc.report_utils import column, get_precision, report_today, validate_company
from treasury.security_cheques.lifecycle import LIVE_STATUSES
from treasury.security_cheques.report_utils import (
	CTX,
	DOCTYPE,
	AmountConverter,
	cheque_columns,
	company_currency_column,
	get_security_cheques,
	make_row,
)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	as_on = getdate(filters.as_on) if filters.as_on else report_today()

	conditions = [
		["company", "=", company],
		["status", "in", LIVE_STATUSES],
		["expiry_date", "<", as_on],
	]
	for fieldname in ("direction", "party_type", "party", "purpose"):
		if filters.get(fieldname):
			conditions.append([fieldname, "=", filters.get(fieldname)])

	converter = AmountConverter(company)
	rows = []
	for cheque in get_security_cheques(conditions):
		row = make_row(cheque, converter, as_on)
		row.days_expired = date_diff(as_on, getdate(cheque.expiry_date))
		rows.append(row)
	rows.append(
		frappe._dict(
			is_total=1,
			indent=0,
			cheque_no=_("Total", context=CTX),
			cheque_count=len(rows),
			base_amount=flt(sum(r.base_amount for r in rows), get_precision()),
			company_currency=converter.company_currency,
		)
	)
	return get_columns(), rows


def get_columns() -> list[dict]:
	return [
		*cheque_columns(),
		column("cheque_count", _("Cheques"), "Int", 70),
		column("expiry_date", _("Expiry Date"), "Date", 100),
		column("days_expired", _("Days Since Expiry"), "Int", 100),
		column("status", _("Status"), width=100),
		column("custody_location", _("Custody Location"), "Link", 140, options="Custody Location"),
		column("custodian", _("Custodian"), "Link", 140, options="User"),
		column("reference_doctype", _("Reference Type"), "Link", 120, options="DocType"),
		column("reference_name", _("Reference Name"), "Dynamic Link", 150, options="reference_doctype"),
		company_currency_column(),
	]
