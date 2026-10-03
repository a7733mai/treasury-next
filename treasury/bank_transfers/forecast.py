"""Bank Transfers in the Bank Position Forecast (treasury.pdc.forecast_sources).

- `get_flows` (hook `treasury_forecast_sources`): Scheduled transfers, on their execution date — a payment or
  the source side of an internal transfer goes out (amount + fee), a receipt comes in (amount - fee), the
  target side of an internal transfer comes in (the received amount).
- `get_summary` (hook `treasury_forecast_summary_sources`): Sent transfers are already in the GL (their
  Payment Entry is posted), so they are not flows (they would be counted twice); one card shows the net amount
  of the Sent transfers the bank has not confirmed yet.

Company currency amounts are converted at the rate of the report's today.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from treasury.bank_facilities.forecast import BANK_CURRENCY, _rate, company_amount
from treasury.bank_transfers import lifecycle
from treasury.pdc.report_utils import can_read, get_precision, read_list, report_today

DOCTYPE = "Bank Transfer"
FIELDS = [
	"name",
	"company",
	"transfer_type",
	"method",
	"bank_account",
	"to_bank_account",
	"currency",
	"to_currency",
	"bank_amount",
	"received_amount",
	"execution_date",
	"party_name",
]


def _movements(row, bank_accounts: set) -> list[tuple[str, str, str, float]]:
	"""(direction, bank account, currency, amount in the account currency) of a transfer on the given banks."""
	movements = []
	source_direction = "in" if row.transfer_type == lifecycle.PARTY_RECEIPT else "out"
	if row.bank_account in bank_accounts:
		movements.append((source_direction, row.bank_account, row.currency, flt(row.bank_amount)))
	if row.transfer_type == lifecycle.INTERNAL and row.to_bank_account in bank_accounts:
		movements.append(("in", row.to_bank_account, row.to_currency, flt(row.received_amount)))
	return movements


def _transfers(filters, status: str, to_date=None) -> list:
	# `filters.system`: company-wide view of the balance guard and alerts (no permission checks)
	conditions = {"company": filters.company, "docstatus": 1, "status": status}
	if to_date:
		conditions["execution_date"] = ["<=", getdate(to_date)]
	return read_list(
		DOCTYPE,
		filters.system,
		filters=conditions,
		fields=FIELDS,
		order_by="execution_date asc, name asc",
	)


def get_flows(filters) -> list[dict]:
	filters = frappe._dict(filters)
	if not filters.bank_accounts or not can_read(DOCTYPE, filters.system):
		return []
	bank_accounts = set(filters.bank_accounts)
	today = report_today()
	rates: dict[str, float] = {}
	flows = []
	for row in _transfers(filters, lifecycle.SCHEDULED, filters.to_date):
		for direction, bank_account, currency, amount in _movements(row, bank_accounts):
			if amount <= 0:
				continue
			flows.append(
				{
					"date": getdate(row.execution_date),
					"direction": direction,
					"bank_account": bank_account,
					"amount": company_amount(filters, amount, currency, row.company, today, rates),
					"amount_in_account_currency": amount,
					"reference_doctype": DOCTYPE,
					"reference_name": row.name,
					"party": row.party_name or row.to_bank_account,
					"description": _("Bank Transfer ({0})").format(_(row.method, context=DOCTYPE)),
				}
			)
	return flows


def get_summary(filters) -> list[dict]:
	"""[{"label", "value", "indicator"}] in the report's view currency (see the module docstring)."""
	filters = frappe._dict(filters)
	if not filters.bank_accounts or not frappe.has_permission(DOCTYPE, "read"):
		return []
	bank_accounts = set(filters.bank_accounts)
	rates: dict[str, float] = {}
	total, found = 0.0, False
	for row in _transfers(filters, lifecycle.SENT):
		for direction, _bank_account, currency, amount in _movements(row, bank_accounts):
			if filters.currency_view == BANK_CURRENCY and currency != filters.currency:
				continue
			rate = 1.0
			if filters.currency_view != BANK_CURRENCY:
				rate = _rate(currency, filters.company, filters.today or report_today(), rates)
			found = True
			total += (amount if direction == "out" else -amount) * rate
	if not found:
		return []
	return [
		{
			"label": _("Transfers Sent, Awaiting Bank Confirmation"),
			"value": flt(total, get_precision()),
			"indicator": "Orange",
		}
	]
