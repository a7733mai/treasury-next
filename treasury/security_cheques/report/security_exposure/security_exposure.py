# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Security Exposure (PLAN.md §7): the guarantees held from others and given to others.

Only live cheques count: incoming Received / In Custody (held from others) and outgoing Issued (given to
others). Amounts are in the company currency; Open / Blank cheques carry no amount and are counted apart.

Views (`group_by`):
- Party: one row per direction and party.
- Reference Document: one row per direction and referenced document (contract, order, tender ...), with
  the Required amount, the Actual (fixed) amount of its cheques and the Shortfall (Required - Actual, never
  negative). The required amount is the document's requirement, entered on each of its cheques: the
  highest one among them is used (not their sum). A cheque without a reference is its own row.

The report summary gives the totals held / given and the Open / Blank counts.
"""

import frappe
from frappe import _
from frappe.utils import flt

from treasury.pdc.report_utils import column, get_precision, validate_company
from treasury.security_cheques.lifecycle import INCOMING, LIVE_BY_DIRECTION, LIVE_STATUSES, OUTGOING
from treasury.security_cheques.report_utils import (
	CTX,
	DOCTYPE,
	AmountConverter,
	base_required,
	company_currency_column,
	get_security_cheques,
	is_open,
)

PARTY = "Party"
REFERENCE = "Reference Document"
GROUP_BY = (PARTY, REFERENCE)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	group_by = filters.group_by or PARTY
	if group_by not in GROUP_BY:
		frappe.throw(
			_("Group By must be one of: {0}.").format(", ".join(_(g, context=CTX) for g in GROUP_BY))
		)

	converter = AmountConverter(company)
	cheques = [c for c in get_security_cheques(get_filters(filters, company)) if is_live(c)]
	rows = group_by_reference(cheques, converter) if group_by == REFERENCE else group_by_party(cheques, converter)
	return get_columns(group_by), rows, None, None, get_summary(rows, converter.company_currency)


def get_filters(filters: frappe._dict, company: str) -> list:
	conditions = [["company", "=", company], ["status", "in", LIVE_STATUSES]]
	for fieldname in ("direction", "party_type", "party", "purpose", "reference_doctype", "reference_name"):
		if filters.get(fieldname):
			conditions.append([fieldname, "=", filters.get(fieldname)])
	return conditions


def is_live(cheque) -> bool:
	return cheque.status in LIVE_BY_DIRECTION.get(cheque.direction, ())


def exposure_label(direction: str) -> str:
	return _("Held from Others") if direction == INCOMING else _("Given to Others")


def new_group(cheque, converter, **values) -> frappe._dict:
	return frappe._dict(
		direction=cheque.direction,
		exposure=exposure_label(cheque.direction),
		fixed_count=0,
		open_count=0,
		amount=0.0,
		company_currency=converter.company_currency,
		**values,
	)


def add_cheque(group: frappe._dict, cheque, converter: AmountConverter):
	if is_open(cheque):
		group.open_count += 1
	else:
		group.fixed_count += 1
		group.amount = flt(group.amount + converter.company_amount(cheque), get_precision())


def group_by_party(cheques, converter: AmountConverter) -> list[frappe._dict]:
	groups: dict[tuple, frappe._dict] = {}
	for cheque in cheques:
		key = (cheque.direction, cheque.party_type, cheque.party or "", cheque.party_name or "")
		if key not in groups:
			groups[key] = new_group(
				cheque,
				converter,
				party_type=cheque.party_type,
				party=cheque.party,
				party_name=cheque.party_name,
			)
		add_cheque(groups[key], cheque, converter)
	return [groups[k] for k in sorted(groups, key=lambda k: (k[0], k[3], k[2]))]


def group_by_reference(cheques, converter: AmountConverter) -> list[frappe._dict]:
	groups: dict[tuple, frappe._dict] = {}
	for cheque in cheques:
		reference = (
			(cheque.reference_doctype, cheque.reference_name)
			if cheque.reference_doctype and cheque.reference_name
			else (DOCTYPE, cheque.name)
		)
		key = (cheque.direction, *reference)
		if key not in groups:
			groups[key] = new_group(
				cheque,
				converter,
				reference_doctype=reference[0],
				reference_name=reference[1],
				party_type=cheque.party_type,
				party=cheque.party,
				party_name=cheque.party_name,
				required_amount=0.0,
			)
		group = groups[key]
		add_cheque(group, cheque, converter)
		group.required_amount = max(group.required_amount, base_required(cheque, converter))

	rows = [groups[k] for k in sorted(groups)]
	precision = get_precision()
	for row in rows:
		row.shortfall = flt(max(row.required_amount - row.amount, 0), precision)
	return rows


def get_summary(rows: list[frappe._dict], currency: str) -> list[dict]:
	def total(direction, field):
		return flt(sum(r.get(field) or 0 for r in rows if r.direction == direction), get_precision())

	summary = [
		{
			"label": _("Held from Others"),
			"value": total(INCOMING, "amount"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Green",
		},
		{
			"label": _("Given to Others"),
			"value": total(OUTGOING, "amount"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Red",
		},
		{
			"label": _("Open / Blank Held"),
			"value": int(total(INCOMING, "open_count")),
			"datatype": "Int",
			"indicator": "Blue",
		},
		{
			"label": _("Open / Blank Given"),
			"value": int(total(OUTGOING, "open_count")),
			"datatype": "Int",
			"indicator": "Orange",
		},
	]
	if rows and "shortfall" in rows[0]:
		summary.append(
			{
				"label": _("Total Shortfall"),
				"value": flt(sum(r.shortfall for r in rows), get_precision()),
				"datatype": "Currency",
				"currency": currency,
				"indicator": "Red",
			}
		)
	return summary


def get_columns(group_by: str) -> list[dict]:
	columns = [
		column("exposure", _("Exposure"), width=130),
		column("direction", _("Direction"), width=90, hidden=1),
	]
	if group_by == REFERENCE:
		columns += [
			column("reference_doctype", _("Reference Type"), "Link", 120, options="DocType"),
			column("reference_name", _("Reference Name"), "Dynamic Link", 160, options="reference_doctype"),
		]
	columns += [
		column("party_type", _("Party Type"), width=90, hidden=1),
		column("party", _("Party", context=CTX), "Dynamic Link", 150, options="party_type"),
		column("party_name", _("Party Name"), width=180),
		column("fixed_count", _("Fixed Cheques"), "Int", 100),
		column("open_count", _("Open / Blank Cheques"), "Int", 120),
		column("amount", _("Amount (Company Currency)"), "Currency", 150, options="company_currency"),
	]
	if group_by == REFERENCE:
		columns += [
			column("required_amount", _("Required Amount"), "Currency", 130, options="company_currency"),
			column("shortfall", _("Shortfall"), "Currency", 120, options="company_currency"),
		]
	columns.append(company_currency_column())
	return columns
