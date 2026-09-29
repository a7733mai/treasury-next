# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Transfer Fees Analysis: the fees booked on the transfers executed in a period (Sent, Completed, Returned;
a fee the bank refunded on a return does not count), per month, bank, bank account or method, with the
amount transferred, the average fee and the fee in percent of the amount (company currency). Months are in
calendar order, the other groups by fees, highest first.

Transfers are read with `frappe.get_list` (permissions and User Permissions apply).
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, get_first_day, getdate

from treasury.bank_transfers import lifecycle
from treasury.pdc.report_utils import (
	CTX,
	column,
	get_precision,
	report_today,
	validate_company,
	validate_date_range,
)

DOCTYPE = "Bank Transfer"
MONTH, BANK, BANK_ACCOUNT, METHOD = "Month", "Bank", "Bank Account", "Method"
GROUP_FIELDS = {MONTH: "execution_date", BANK: "bank", BANK_ACCOUNT: "bank_account", METHOD: "method"}
STATUSES = (lifecycle.SENT, lifecycle.COMPLETED, lifecycle.RETURNED)
FIELDS = [
	"name",
	"execution_date",
	"bank",
	"bank_account",
	"method",
	"base_amount",
	"base_fee_amount",
	"fees_refunded",
]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	today = report_today()
	from_date = getdate(filters.from_date or get_first_day(today))
	to_date = getdate(filters.to_date or today)
	validate_date_range(from_date, to_date)
	group_by = filters.group_by or MONTH
	if group_by not in GROUP_FIELDS:
		labels = ", ".join(_(g, context=CTX) for g in GROUP_FIELDS)
		frappe.throw(_("Group By must be one of: {0}.").format(labels))

	conditions = {
		"company": company,
		"docstatus": 1,
		"status": ["in", STATUSES],
		"execution_date": ["between", [from_date, to_date]],
	}
	for fieldname in ("bank_account", "method"):
		if filters.get(fieldname):
			conditions[fieldname] = filters.get(fieldname)
	rows = frappe.get_list(
		DOCTYPE,
		filters=conditions,
		fields=FIELDS,
		order_by="execution_date asc, name asc",
	)
	data = group_rows(rows, group_by)
	currency = frappe.get_cached_value("Company", company, "default_currency")
	for row in data:
		row.company_currency = currency
	total = total_row(data, currency)
	return get_columns(), [*data, total] if data else [], None, get_chart(data, currency)


def group_rows(rows: list, group_by: str) -> list[frappe._dict]:
	precision = get_precision()
	groups: dict[str, frappe._dict] = {}
	for row in rows:
		key = group_key(row, group_by)
		group = groups.setdefault(key, frappe._dict(group=key, transfers=0, amount=0.0, fees=0.0))
		group.transfers += 1
		group.amount += flt(row.base_amount)
		group.fees += 0 if cint(row.fees_refunded) else flt(row.base_fee_amount)
	data = list(groups.values())
	for group in data:
		finish(group, precision)
		if group_by == METHOD:
			group.group = _(group.group, context=DOCTYPE)
	if group_by == MONTH:
		data.sort(key=lambda g: g.group)
	else:
		data.sort(key=lambda g: (-g.fees, g.group))
	return data


def group_key(row, group_by: str) -> str:
	if group_by == MONTH:
		return getdate(row.execution_date).strftime("%Y-%m")
	return row.get(GROUP_FIELDS[group_by]) or "-"


def finish(group: frappe._dict, precision: int):
	group.amount = flt(group.amount, precision)
	group.fees = flt(group.fees, precision)
	group.average_fee = flt(group.fees / group.transfers, precision) if group.transfers else 0
	group.fee_percent = flt(group.fees / group.amount * 100, 2) if group.amount else 0


def total_row(data: list, currency: str) -> frappe._dict:
	total = frappe._dict(
		group=_("Total", context=CTX),
		transfers=sum(g.transfers for g in data),
		amount=sum(g.amount for g in data),
		fees=sum(g.fees for g in data),
		is_total=1,
		bold=1,
		company_currency=currency,
	)
	finish(total, get_precision())
	return total


def get_chart(data: list, currency: str) -> dict | None:
	if not data:
		return None
	return {
		"data": {
			"labels": [g.group for g in data],
			"datasets": [{"name": _("Fees", context=DOCTYPE), "values": [g.fees for g in data]}],
		},
		"type": "bar",
		"fieldtype": "Currency",
		"options": currency,
		"colors": ["#e8590c"],
	}


def get_columns() -> list[dict]:
	def money(fieldname, label, width=150):
		return column(fieldname, label, "Currency", width, options="company_currency")

	return [
		column("group", _("Group", context=CTX), width=180),
		column("transfers", _("Transfers"), "Int", 90),
		money("amount", _("Amount (Company Currency)", context=DOCTYPE)),
		money("fees", _("Fees (Company Currency)", context=DOCTYPE)),
		money("average_fee", _("Average Fee", context=DOCTYPE), 120),
		column("fee_percent", _("Fee %", context=DOCTYPE), "Percent", 90),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
	]
