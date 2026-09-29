# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Bank Transfer Register: the transfers executed (or scheduled) in a period, with their amounts, fee, status
and Payment Entry. Draft and submitted transfers; cancelled ones only when the Cancelled status is chosen.
A bank account matches the source or the target bank of a transfer.

Transfers are read with `frappe.get_list` (permissions and User Permissions apply).
"""

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, getdate

from treasury.bank_transfers import lifecycle
from treasury.pdc.report_utils import (
	CTX,
	column,
	get_precision,
	report_today,
	validate_company,
	validate_date_range,
	validate_party,
)

DOCTYPE = "Bank Transfer"
FIELDS = [
	"name",
	"execution_date",
	"transfer_type",
	"method",
	"status",
	"party_type",
	"party",
	"party_name",
	"bank_account",
	"to_bank_account",
	"currency",
	"amount",
	"fee_amount",
	"bank_amount",
	"base_amount",
	"base_fee_amount",
	"scope",
	"bank_reference",
	"payment_entry",
	"completion_date",
	"return_date",
]
# Transfers whose money moved (their Payment Entry is posted and not reversed)
MOVED_STATUSES = (lifecycle.SENT, lifecycle.COMPLETED)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DOCTYPE)
	today = report_today()
	from_date = getdate(filters.from_date or get_first_day(today))
	to_date = getdate(filters.to_date or today)
	validate_date_range(from_date, to_date)

	conditions = {"company": company, "execution_date": ["between", [from_date, to_date]]}
	if filters.status == lifecycle.CANCELLED:
		conditions["docstatus"] = 2
	else:
		conditions["docstatus"] = ["<", 2]
		if filters.status:
			conditions["status"] = filters.status
	for fieldname in ("method", "transfer_type"):
		if filters.get(fieldname):
			conditions[fieldname] = filters.get(fieldname)
	if filters.party:
		validate_party(filters.party_type, filters.party)
		conditions.update(party_type=filters.party_type, party=filters.party)
	or_filters = None
	if filters.bank_account:
		or_filters = {"bank_account": filters.bank_account, "to_bank_account": filters.bank_account}

	rows = frappe.get_list(
		DOCTYPE,
		filters=conditions,
		or_filters=or_filters,
		fields=FIELDS,
		order_by="execution_date asc, name asc",
	)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	for row in rows:
		row.update(
			bank_transfer=row.name,
			transfer_type_label=_(row.transfer_type, context=DOCTYPE),
			method_label=_(row.method, context=DOCTYPE),
			status_label=_(row.status, context=DOCTYPE),
			scope_label=_(row.scope, context=DOCTYPE) if row.scope else "",
			company_currency=company_currency,
		)
	return get_columns(), rows, None, None, get_summary(rows, company_currency)


def get_summary(rows: list, currency: str) -> list[dict]:
	precision = get_precision()
	moved = [r for r in rows if r.status in MOVED_STATUSES]
	fees = [r for r in rows if r.status in (*MOVED_STATUSES, lifecycle.RETURNED)]
	return [
		{"value": len(rows), "label": _("Transfers"), "datatype": "Int", "indicator": "Blue"},
		{
			"value": flt(sum(flt(r.base_amount) for r in moved), precision),
			"label": _("Sent and Completed (Company Currency)"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Green",
		},
		{
			"value": flt(sum(flt(r.base_fee_amount) for r in fees), precision),
			"label": _("Fees Booked (Company Currency)"),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Orange",
		},
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, options="currency", width=120):
		return column(fieldname, label, "Currency", width, options=options)

	return [
		column("bank_transfer", _("Bank Transfer"), "Link", 150, options=DOCTYPE),
		column("execution_date", _("Execution Date", context=DOCTYPE), "Date", 100),
		column("transfer_type_label", _("Transfer Type", context=DOCTYPE), width=120),
		column("method_label", _("Method", context=DOCTYPE), width=80),
		column("status_label", _("Status", context="Treasury"), width=100),
		column("party_type", _("Party Type"), width=90),
		column("party_name", _("Party Name"), width=160),
		column("bank_account", _("Bank Account", context="Treasury"), "Link", 170, options="Bank Account"),
		column("to_bank_account", _("To Bank Account", context=DOCTYPE), "Link", 170, options="Bank Account"),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		money("amount", _("Amount", context="Treasury")),
		money("fee_amount", _("Fee", context=DOCTYPE), width=100),
		money("bank_amount", _("Bank Debit / Credit", context=DOCTYPE)),
		money("base_amount", _("Amount (Company Currency)", context=DOCTYPE), "company_currency", 150),
		column("scope_label", _("Scope", context=DOCTYPE), width=100),
		column("bank_reference", _("Bank Reference", context=DOCTYPE), width=130),
		column("payment_entry", _("Payment Entry", context=CTX), "Link", 160, options="Payment Entry"),
		column("completion_date", _("Completion Date", context=DOCTYPE), "Date", 100),
		column("return_date", _("Return Date", context=DOCTYPE), "Date", 100),
		column(
			"company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1
		),
	]
