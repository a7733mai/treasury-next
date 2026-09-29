# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""PDC Maturity / Aging (PLAN.md §5.4): open cheques bucketed by their effective due date relative to
"As On", grouped by bank account or party, in the company currency.

Open = still in circulation (Created, Delivered, Received, Deposited) and not bank posted.
Buckets (days from As On to the effective due date): Overdue (< 0), 0-7, 8-30, 31-60, 61-90, 90+.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, getdate

from treasury.pdc.report_utils import (
	BANK_POSTED,
	CTX,
	OPEN_STATUSES,
	OUTGOING,
	AmountConverter,
	get_cheques,
	get_precision,
	report_today,
	validate_company,
)

BUCKETS = (
	("overdue", None, -1),
	("days_0_7", 0, 7),
	("days_8_30", 8, 30),
	("days_31_60", 31, 60),
	("days_61_90", 61, 90),
	("days_90_plus", 91, None),
)
GROUP_BY_BANK, GROUP_BY_PARTY = "Bank Account", "Party"


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company)
	as_on = getdate(filters.as_on or report_today())
	direction = filters.direction or OUTGOING
	group_by = filters.group_by or GROUP_BY_BANK

	conditions = {
		"company": company,
		"direction": direction,
		"operational_status": ["in", list(OPEN_STATUSES)],
		"accounting_status": ["!=", BANK_POSTED],
	}
	if filters.bank_account:
		conditions["bank_account"] = filters.bank_account

	converter = AmountConverter(company, as_on)
	groups: dict[str, frappe._dict] = {}
	for cheque in get_cheques(conditions):
		key, label = group_key(cheque, group_by)
		row = groups.setdefault(key, new_row(key, label, converter.company_currency))
		amount = converter.company_amount(cheque)
		row[bucket_of(date_diff(cheque.effective_due_date, as_on))] += amount
		row.total += amount
		row.cheque_count += 1

	precision = get_precision()
	data = [
		round_row(groups[k], precision) for k in sorted(groups, key=lambda k: groups[k].group_label or "")
	]
	if data:
		data.append(round_row(total_row(data, converter.company_currency), precision))
	return get_columns(group_by), data, None, get_chart(data[:-1])


def bucket_of(days: int) -> str:
	for fieldname, low, high in BUCKETS:
		if (low is None or days >= low) and (high is None or days <= high):
			return fieldname
	return BUCKETS[-1][0]


def group_key(cheque, group_by: str) -> tuple[str, str]:
	if group_by == GROUP_BY_PARTY:
		key = f"{cheque.party_type}::{cheque.party}"
		return key, cheque.party_name or cheque.party
	return cheque.bank_account or "", cheque.bank_account or _("Unassigned Bank")


def new_row(key: str, label: str, currency: str) -> frappe._dict:
	row = frappe._dict(group_key=key, group_label=label, currency=currency, total=0.0, cheque_count=0)
	for fieldname, _low, _high in BUCKETS:
		row[fieldname] = 0.0
	return row


def total_row(rows: list[frappe._dict], currency: str) -> frappe._dict:
	row = new_row("", _("Total", context=CTX), currency)
	row.is_total = 1
	for fieldname in [b[0] for b in BUCKETS] + ["total", "cheque_count"]:
		row[fieldname] = sum(r[fieldname] for r in rows)
	return row


def round_row(row: frappe._dict, precision: int) -> frappe._dict:
	for fieldname in [b[0] for b in BUCKETS] + ["total"]:
		row[fieldname] = flt(row[fieldname], precision)
	return row


def bucket_labels() -> dict[str, str]:
	return {
		"overdue": _("Overdue"),
		"days_0_7": _("0-7 Days"),
		"days_8_30": _("8-30 Days"),
		"days_31_60": _("31-60 Days"),
		"days_61_90": _("61-90 Days"),
		"days_90_plus": _("Over 90 Days"),
	}


def get_columns(group_by: str) -> list[dict]:
	columns = [
		{
			"fieldname": "group_label",
			"label": _("Party Name") if group_by == GROUP_BY_PARTY else _("Bank Account"),
			"fieldtype": "Data",
			"width": 220,
		}
	]
	for fieldname, label in bucket_labels().items():
		columns.append(
			{
				"fieldname": fieldname,
				"label": label,
				"fieldtype": "Currency",
				"options": "currency",
				"width": 120,
			}
		)
	columns += [
		{
			"fieldname": "total",
			"label": _("Total", context=CTX),
			"fieldtype": "Currency",
			"options": "currency",
			"width": 130,
		},
		{"fieldname": "cheque_count", "label": _("Cheques"), "fieldtype": "Int", "width": 80},
		{
			"fieldname": "currency",
			"label": _("Currency"),
			"fieldtype": "Link",
			"options": "Currency",
			"hidden": 1,
		},
	]
	return columns


def get_chart(rows: list[frappe._dict]) -> dict | None:
	if not rows:
		return None
	return {
		"data": {
			"labels": [r.group_label for r in rows],
			"datasets": [
				{"name": label, "values": [r[fieldname] for r in rows]}
				for fieldname, label in bucket_labels().items()
			],
		},
		"type": "bar",
		"barOptions": {"stacked": 1},
		"fieldtype": "Currency",
		"options": rows[0].currency,
	}
