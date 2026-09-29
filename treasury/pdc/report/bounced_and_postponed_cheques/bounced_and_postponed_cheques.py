# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bounced and Postponed Cheques (PLAN.md §5.5), one view at a time.

Bounced: one row per bounce (a re-presented cheque may bounce again), from the "Bounced" status log rows
whose `event_date` (the bounce date the user entered) is in the range. Bounce charges come from the
"Bounce Charges Posted" row that follows the bounce.

Postponed: one row per postponement (PDC Postponement), dated when it was recorded, with the number of
postponements of the same party in the result as a frequency indicator.
"""

from collections import Counter

import frappe
from frappe import _
from frappe.utils import add_months, date_diff, flt, getdate

import erpnext
from treasury.pdc.report_utils import (
	CTX,
	DOCTYPE,
	cheque_columns,
	column,
	get_cheques,
	get_precision,
	report_today,
	validate_company,
	validate_date_range,
)

BOUNCED, POSTPONED = "Bounced", "Postponed"
BOUNCE_CHARGES_POSTED, REPRESENTED = "Bounce Charges Posted", "Re-presented"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company)
	today = report_today()
	from_date = getdate(filters.from_date or add_months(today, -3))
	to_date = getdate(filters.to_date or today)
	validate_date_range(from_date, to_date)
	view = filters.view or BOUNCED
	if view not in (BOUNCED, POSTPONED):
		frappe.throw(_("View must be one of: {0}.").format(", ".join((_(BOUNCED), _(POSTPONED)))))

	conditions = {"company": company}
	if filters.direction:
		conditions["direction"] = filters.direction
	if filters.party_type:
		conditions["party_type"] = filters.party_type
	if filters.party:
		conditions["party"] = filters.party

	if view == BOUNCED:
		rows = get_bounced_rows(conditions, from_date, to_date)
		company_currency = erpnext.get_company_currency(company)
		for row in rows:
			row.company_currency = company_currency
		return get_bounced_columns(), rows

	conditions["postponement_count"] = [">", 0]
	cheques = {c.name: c for c in get_cheques(conditions)}
	return get_postponed_columns(), get_postponed_rows(cheques, from_date, to_date)


def get_child_rows(doctype: str, parentfield: str, cheque_names: list[str], fields: list[str], **filters):
	"""History rows of cheques the user may read (the parents come from `frappe.get_list`)."""
	if not cheque_names:
		return []
	return frappe.get_all(
		doctype,
		filters={"parenttype": DOCTYPE, "parentfield": parentfield, "parent": ["in", cheque_names], **filters},
		fields=["parent", "idx", *fields],
		order_by="parent asc, idx asc",
	)


# ---------------------------------------------------------------------------
# Bounced
# ---------------------------------------------------------------------------


def get_bounced_rows(conditions: dict, from_date, to_date) -> list[frappe._dict]:
	"""Bounces dated in [from_date, to_date] of the cheques matching `conditions` that the user may read.

	The status log is queried first (indexed on `event` / `event_date`), so only the bounced cheques are
	loaded; their parents then go through `frappe.get_list` (permissions and User Permissions apply)."""
	bounces = frappe.get_all(
		"PDC Status Log",
		filters={
			"parenttype": DOCTYPE,
			"parentfield": "status_log",
			"event": BOUNCED,
			"event_date": ["between", [from_date, to_date]],
		},
		fields=["parent", "idx", "event_date", "remarks"],
		order_by="parent asc, idx asc",
	)
	if not bounces:
		return []
	cheques = {
		c.name: c for c in get_cheques({**conditions, "name": ["in", sorted({b.parent for b in bounces})]})
	}
	history = get_bounce_history(sorted(cheques))

	precision = get_precision()
	rows = []
	for bounce in bounces:
		cheque = cheques.get(bounce.parent)
		if not cheque:
			continue
		charges, represented = bounce_follow_up(bounce, history.get(bounce.parent, []))
		rows.append(
			frappe._dict(
				cheque=cheque.name,
				direction=cheque.direction,
				party_type=cheque.party_type,
				party=cheque.party,
				party_name=cheque.party_name,
				bank_account=cheque.bank_account,
				cheque_no=cheque.cheque_no,
				amount=flt(cheque.amount, precision),
				currency=cheque.currency,
				effective_due_date=cheque.effective_due_date,
				bounce_date=getdate(bounce.event_date),
				reason=bounce.remarks,
				charges=flt(charges, precision),
				represented=represented,
				operational_status=cheque.operational_status,
			)
		)
	rows.sort(key=lambda r: (r.bounce_date, r.cheque))
	return rows


def get_bounce_history(cheque_names: list[str]) -> dict[str, list]:
	"""Per cheque, its Bounced / Bounce Charges Posted / Re-presented log rows in order; bounce charges rows
	carry the charged amount (`charges`, company currency) of their Journal Entry."""
	history: dict[str, list] = {}
	for log in get_child_rows(
		"PDC Status Log",
		"status_log",
		cheque_names,
		["event", "voucher_no"],
		event=["in", [BOUNCED, BOUNCE_CHARGES_POSTED, REPRESENTED]],
	):
		history.setdefault(log.parent, []).append(log)

	journals = [
		log.voucher_no for logs in history.values() for log in logs if log.event == BOUNCE_CHARGES_POSTED
	]
	totals = {}
	if journals:
		totals = dict(
			frappe.get_all(
				"Journal Entry",
				filters={"name": ["in", journals], "docstatus": 1},
				fields=["name", "total_debit"],
				as_list=True,
			)
		)
	for logs in history.values():
		for log in logs:
			log.charges = flt(totals.get(log.voucher_no)) if log.event == BOUNCE_CHARGES_POSTED else 0.0
	return history


def bounce_follow_up(bounce, history: list) -> tuple[float, int]:
	"""(bounce charges, re-presented?) of `bounce`: the rows after it, up to the next bounce."""
	charges, represented = 0.0, 0
	for log in history:
		if log.idx <= bounce.idx:
			continue
		if log.event == BOUNCED:
			break
		if log.event == BOUNCE_CHARGES_POSTED:
			charges += log.charges
		elif log.event == REPRESENTED:
			represented = 1
	return charges, represented


def get_bounced_columns() -> list[dict]:
	return [
		*cheque_columns(),
		column("effective_due_date", _("Effective Due Date"), "Date"),
		column("bounce_date", _("Bounce Date"), "Date"),
		column("reason", _("Reason"), width=220),
		column("charges", _("Bounce Charges"), "Currency", 120, options="company_currency"),
		column("represented", _("Re-presented"), "Check", 100),
		column("operational_status", _("Operational Status"), width=120),
		column("company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1),
	]


# ---------------------------------------------------------------------------
# Postponed
# ---------------------------------------------------------------------------


def get_postponed_rows(cheques: dict, from_date, to_date) -> list[frappe._dict]:
	precision = get_precision()
	rows = []
	for postponement in get_child_rows(
		"PDC Postponement",
		"postponements",
		list(cheques),
		["datetime", "old_due_date", "new_due_date", "reason", "contact_person", "approved_by"],
	):
		postponed_on = getdate(postponement.datetime)
		if not from_date <= postponed_on <= to_date:
			continue
		cheque = cheques[postponement.parent]
		rows.append(
			frappe._dict(
				cheque=cheque.name,
				direction=cheque.direction,
				party_type=cheque.party_type,
				party=cheque.party,
				party_name=cheque.party_name,
				bank_account=cheque.bank_account,
				cheque_no=cheque.cheque_no,
				amount=flt(cheque.amount, precision),
				currency=cheque.currency,
				postponed_on=postponed_on,
				old_due_date=postponement.old_due_date,
				new_due_date=postponement.new_due_date,
				days_postponed=date_diff(postponement.new_due_date, postponement.old_due_date),
				reason=postponement.reason,
				contact_person=postponement.contact_person,
				approved_by=postponement.approved_by,
				_sort=(postponed_on, postponement.datetime, cheque.name, postponement.idx),
			)
		)

	frequency = Counter((r.party_type, r.party) for r in rows)
	for row in rows:
		row.party_postponements = frequency[(row.party_type, row.party)]
	rows.sort(key=lambda r: r.pop("_sort"))
	return rows


def get_postponed_columns() -> list[dict]:
	return [
		*cheque_columns(),
		column("postponed_on", _("Postponed On"), "Date"),
		column("old_due_date", _("Old Due Date"), "Date"),
		column("new_due_date", _("New Due Date"), "Date"),
		column("days_postponed", _("Days Postponed"), "Int", 90),
		column("reason", _("Reason"), width=200),
		column("contact_person", _("Contact Person"), width=140),
		column("approved_by", _("Approved By"), "Link", 140, options="User"),
		column("party_postponements", _("Party Postponements"), "Int", 110),
	]

