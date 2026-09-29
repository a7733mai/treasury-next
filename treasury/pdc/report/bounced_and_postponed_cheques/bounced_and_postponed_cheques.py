# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bounced and Postponed Cheques (PLAN.md §5.5), one view at a time.

Bounced: one row per bounce (a re-presented cheque may bounce again), from the status log. The bounce date
is the posting date of the reversal (or bounce charges) Journal Entry of that bounce — the date the user
gave — and the date the bounce was recorded when no dated voucher exists (a cheque bounced before posting,
or On Bounce = Cancel Payment Entry without bounce charges).

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
# status log events written by the same bounce action, right after its "Bounced" row
BOUNCE_FOLLOW_UP_EVENTS = ("Voucher Reversed", "Voucher Cancelled", "Reversed", "Bounce Charges Posted")
DATED_BOUNCE_VOUCHER_EVENTS = ("Voucher Reversed", "Bounce Charges Posted")


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
	if view == POSTPONED:
		conditions["postponement_count"] = [">", 0]

	cheques = {c.name: c for c in get_cheques(conditions)}
	if view == BOUNCED:
		rows = get_bounced_rows(cheques, from_date, to_date)
		company_currency = erpnext.get_company_currency(company)
		for row in rows:
			row.company_currency = company_currency
		return get_bounced_columns(), rows
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


def get_bounced_rows(cheques: dict, from_date, to_date) -> list[frappe._dict]:
	bounced = {
		log.parent
		for log in get_child_rows("PDC Status Log", "status_log", list(cheques), [], event="Bounced")
	}
	logs: dict[str, list] = {}
	for log in get_child_rows(
		"PDC Status Log",
		"status_log",
		sorted(bounced),
		["event", "datetime", "remarks", "voucher_type", "voucher_no"],
	):
		logs.setdefault(log.parent, []).append(log)

	precision = get_precision()
	rows = []
	for name, history in logs.items():
		cheque = cheques[name]
		for index, log in enumerate(history):
			if log.event != "Bounced":
				continue
			bounce_date, charges = bounce_details(log, history[index + 1 :])
			if not from_date <= bounce_date <= to_date:
				continue
			rows.append(
				frappe._dict(
					cheque=name,
					direction=cheque.direction,
					party_type=cheque.party_type,
					party=cheque.party,
					party_name=cheque.party_name,
					bank_account=cheque.bank_account,
					cheque_no=cheque.cheque_no,
					amount=flt(cheque.amount, precision),
					currency=cheque.currency,
					effective_due_date=cheque.effective_due_date,
					bounce_date=bounce_date,
					reason=log.remarks,
					charges=flt(charges, precision),
					represented=1 if any(r.event == "Re-presented" for r in history[index + 1 :]) else 0,
					operational_status=cheque.operational_status,
				)
			)
	rows.sort(key=lambda r: (r.bounce_date, r.cheque))
	return rows


def bounce_details(log, following: list) -> tuple:
	"""(bounce date, bounce charges in the company currency) of the bounce logged in `log`."""
	bounce_date, charges = None, 0.0
	for row in following:
		if row.event not in BOUNCE_FOLLOW_UP_EVENTS:
			break
		if row.voucher_type != "Journal Entry" or row.event not in DATED_BOUNCE_VOUCHER_EVENTS:
			continue
		journal = frappe.db.get_value(
			"Journal Entry", row.voucher_no, ["posting_date", "total_debit"], as_dict=True
		)
		if not journal:
			continue
		bounce_date = bounce_date or getdate(journal.posting_date)
		if row.event == "Bounce Charges Posted":
			charges += flt(journal.total_debit)
	return bounce_date or getdate(log.datetime), charges


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

