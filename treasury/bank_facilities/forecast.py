"""Bank Facilities in the Bank Position Forecast (treasury.pdc.forecast_sources).

- `get_flows` (hook `treasury_forecast_sources`): the unpaid part (principal + interest / profit) of every
  installment of the submitted drawdowns, as an outflow on its due date from the drawdown's bank account.
  Paid installments and prepayment rows never appear; a partly paid one shows its unpaid rest. Drawdowns are
  submitted on their own date, so they are actual movements, never expected ones. An installment with a live
  installment cheque (PDC to Lender) is left out: the cheque itself is the expected outflow (spec 7g §3.12).
- `get_summary` (hook `treasury_forecast_summary_sources`): "Available Overdraft", the available amount of
  the Overdraft sub-limits of the Active facilities whose Default Bank Account is in the report's scope
  (treasury.bank_facilities.utilisation).

Amounts in the company currency are converted at the rate of the report's today.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from treasury.accounting import posting
from treasury.bank_facilities.facility import ACTIVE
from treasury.bank_facilities.utilisation import OVERDRAFT, get_utilisation
from treasury.pdc.report_utils import can_read, get_precision, read_list, report_today

DRAWDOWN = "Facility Drawdown"
SCHEDULE = "Facility Repayment Schedule"
BANK_CURRENCY = "Bank Currency"


def _rate(currency: str, company: str, date, cache: dict) -> float:
	if currency not in cache:
		cache[currency] = posting.get_cheque_exchange_rate(currency, company, date)
	return cache[currency]


def company_amount(filters, amount: float, currency: str, company: str, date, cache: dict) -> float | None:
	"""A flow's `amount` in the company currency; None for the per-bank positions (`per_bank`, treasury.pdc.
	bank_positions), which use the account currency only: no exchange rate lookup there."""
	if filters.get("per_bank"):
		return None
	return flt(amount * _rate(currency, company, date, cache), get_precision())


def get_flows(filters) -> list[dict]:
	filters = frappe._dict(filters)
	if not filters.bank_accounts or not can_read(DRAWDOWN, filters.system):
		return []
	precision = get_precision()
	today = report_today()
	drawdown = frappe.qb.DocType(DRAWDOWN)
	row = frappe.qb.DocType(SCHEDULE)
	query = (
		frappe.qb.from_(row)
		.join(drawdown)
		.on((drawdown.name == row.parent) & (row.parenttype == DRAWDOWN))
		.select(
			drawdown.name,
			drawdown.company,
			drawdown.facility,
			drawdown.bank,
			drawdown.lender_name,
			drawdown.limit_type,
			drawdown.currency,
			drawdown.bank_account,
			row.installment_no,
			row.pdc_cheque,
			row.due_date,
			row.principal,
			row.interest,
			row.paid_principal,
			row.paid_interest,
		)
		.where(
			(drawdown.docstatus == 1)
			& (drawdown.company == filters.company)
			& (drawdown.bank_account.isin(list(filters.bank_accounts)))
			& (row.is_prepayment == 0)
			& (row.status != "Paid")
		)
	)
	if filters.to_date:
		query = query.where(row.due_date <= getdate(filters.to_date))
	# only the drawdowns the user may read
	readable = set(
		read_list(
			DRAWDOWN, filters.system, filters={"company": filters.company, "docstatus": 1}, pluck="name"
		)
	)
	rates: dict[str, float] = {}
	flows = []
	rows = query.run(as_dict=True)
	live = set(
		frappe.get_all(
			"PDC Cheque",
			filters={
				"name": ["in", sorted({r.pdc_cheque for r in rows if r.pdc_cheque}) or [""]],
				"operational_status": ["in", ["Created", "Delivered"]],
			},
			pluck="name",
		)
	)
	for r in rows:
		if r.name not in readable or r.pdc_cheque in live:
			continue
		unpaid = flt(
			max(flt(r.principal) - flt(r.paid_principal), 0) + max(flt(r.interest) - flt(r.paid_interest), 0),
			precision,
		)
		if unpaid <= 0:
			continue
		flows.append(
			{
				"date": getdate(r.due_date),
				"direction": "out",
				"bank_account": r.bank_account,
				"amount": company_amount(filters, unpaid, r.currency, r.company, today, rates),
				"amount_in_account_currency": unpaid,
				"reference_doctype": DRAWDOWN,
				"reference_name": r.name,
				"party": r.lender_name or r.bank,
				"description": (_("Rental {0}") if r.limit_type == "Ijarah" else _("Installment {0}")).format(
					r.installment_no
				),
			}
		)
	return flows


def get_summary(filters) -> list[dict]:
	"""[{"label", "value"}] in the report's view currency (see the module docstring)."""
	filters = frappe._dict(filters)
	if not filters.bank_accounts or not frappe.has_permission("Bank Facility", "read"):
		return []
	facilities = frappe.get_list(
		"Bank Facility",
		filters={
			"company": filters.company,
			"status": ACTIVE,
			"default_bank_account": ["in", list(filters.bank_accounts)],
		},
		fields=["name", "currency"],
	)
	rates: dict[str, float] = {}
	total, found = 0.0, False
	for facility in facilities:
		if filters.currency_view == BANK_CURRENCY and facility.currency != filters.currency:
			continue
		rows = get_utilisation(facility.name, OVERDRAFT).limits
		if not rows:
			continue
		found = True
		rate = 1.0
		if filters.currency_view != BANK_CURRENCY:
			rate = _rate(facility.currency, filters.company, filters.today, rates)
		total += flt(rows[0].available) * rate
	if not found:
		return []
	return [{"label": _("Available Overdraft"), "value": total, "indicator": "Blue"}]
