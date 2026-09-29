# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Security Cheques by Custody (PLAN.md §7): the incoming security cheques we hold, per custody location and
custodian — a header row per location / custodian (count, company currency total), followed by its cheques.

Received cheques not put into custody yet are grouped under "No Custody Location".
"""

import frappe
from frappe import _
from frappe.utils import flt

from treasury.pdc.report_utils import column, get_precision, report_today, validate_company
from treasury.security_cheques.lifecycle import INCOMING, LIVE_BY_DIRECTION
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

	conditions = [
		["company", "=", company],
		["direction", "=", INCOMING],
		["status", "in", LIVE_BY_DIRECTION[INCOMING]],
	]
	if filters.custody_location:
		conditions.append(["custody_location", "=", filters.custody_location])
	if filters.custodian:
		conditions.append(["custodian", "=", filters.custodian])

	converter = AmountConverter(company)
	today = report_today()
	rows = [make_row(c, converter, today) for c in get_security_cheques(conditions)]
	data = group_rows(rows)
	data.append(
		frappe._dict(
			is_total=1,
			indent=0,
			group_label=_("Total", context=CTX),
			cheque_count=len(rows),
			base_amount=flt(sum(r.base_amount for r in rows), get_precision()),
			company_currency=converter.company_currency,
		)
	)
	return get_columns(), data


def group_rows(rows: list[frappe._dict]) -> list[frappe._dict]:
	groups: dict[tuple, list] = {}
	for row in rows:
		groups.setdefault((row.custody_location or "", row.custodian or ""), []).append(row)

	data = []
	for location, custodian in sorted(groups):
		members = groups[(location, custodian)]
		label = location or _("No Custody Location")
		data.append(
			frappe._dict(
				group_label=f"{label} / {custodian}" if custodian else label,
				custody_location=location or None,
				custodian=custodian or None,
				is_group=1,
				indent=0,
				cheque_count=len(members),
				base_amount=flt(sum(r.base_amount for r in members), get_precision()),
				company_currency=members[0].company_currency,
			)
		)
		for row in members:
			row.indent = 1
			data.append(row)
	return data


def get_columns() -> list[dict]:
	return [
		column("group_label", _("Custody Location / Custodian"), width=230),
		*cheque_columns(),
		column("cheque_count", _("Cheques"), "Int", 70),
		column("expiry_date", _("Expiry Date"), "Date", 100),
		column("days_to_expiry", _("Days to Expiry"), "Int", 90),
		column("status", _("Status"), width=100),
		column("custody_location", _("Custody Location"), "Link", 140, options="Custody Location"),
		column("custodian", _("Custodian"), "Link", 140, options="User"),
		company_currency_column(),
	]
