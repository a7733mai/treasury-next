# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Facility Repayment Schedule Report (PLAN.md §8): upcoming and overdue installments of the drawdowns.

One row per installment due in [From Date, To Date] (with "Include Overdue", the overdue installments due
before From Date too), with the principal / interest split, what was paid, what is unpaid and the status on
the reports' today (Pending / Partially Paid / Overdue / Paid — Paid ones only with "Include Paid").
Unpaid amounts are also given in the company currency (today's rate).
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate

from treasury.bank_facilities.report_utils import (
	DRAWDOWN,
	FACILITY,
	CompanyConverter,
	get_installments,
	report_today,
)
from treasury.bank_facilities.repayment import OVERDUE, PAID
from treasury.pdc.report_utils import CTX, column, get_precision, validate_company, validate_date_range

DEFAULT_DAYS = 90


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, DRAWDOWN)
	today = report_today()
	from_date = getdate(filters.from_date or today)
	to_date = getdate(filters.to_date or add_days(from_date, DEFAULT_DAYS))
	validate_date_range(from_date, to_date)
	include_overdue = 1 if filters.get("include_overdue") is None else cint(filters.include_overdue)
	include_paid = cint(filters.include_paid)

	rows = get_installments(
		company,
		to_date=to_date,
		from_date=None if include_overdue else from_date,
		facility=filters.facility,
		bank=filters.bank,
		drawdown=filters.drawdown,
		unpaid_only=not include_paid,
	)
	converter = CompanyConverter(company)
	data = []
	for row in rows:
		due_date = getdate(row.due_date)
		# before From Date: only the overdue ones
		if due_date < from_date and row.status != OVERDUE:
			continue
		if filters.status and row.status != filters.status:
			continue
		row.status_label = _(row.status, context=CTX)
		row.company_currency = converter.company_currency
		row.unpaid_company = converter.convert(row.unpaid, row.currency)
		row.limit_type_label = _(row.limit_type, context=FACILITY)
		data.append(row)
	return get_columns(), data, None, None, get_summary(data, converter.company_currency)


def get_summary(rows: list, currency: str) -> list[dict]:
	precision = get_precision()
	converter_rows = [r for r in rows if r.status != PAID]
	overdue = [r for r in converter_rows if r.status == OVERDUE]
	return [
		{
			"label": _("Unpaid (Company Currency)"),
			"value": flt(sum(r.unpaid_company for r in converter_rows), precision),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Blue",
		},
		{
			"label": _("Overdue (Company Currency)"),
			"value": flt(sum(r.unpaid_company for r in overdue), precision),
			"datatype": "Currency",
			"currency": currency,
			"indicator": "Red",
		},
		{"label": _("Overdue Installments"), "value": len(overdue), "datatype": "Int", "indicator": "Red"},
		{
			"label": _("Unpaid Installments"),
			"value": len(converter_rows),
			"datatype": "Int",
			"indicator": "Orange",
		},
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, options="currency", width=120):
		return column(fieldname, label, "Currency", width, options=options)

	return [
		column("due_date", _("Due Date"), "Date", 100),
		column("drawdown", _("Facility Drawdown"), "Link", 150, options=DRAWDOWN),
		column("facility", _("Bank Facility"), "Link", 150, options=FACILITY),
		column("bank", _("Bank"), "Link", 130, options="Bank"),
		column("limit_type_label", _("Limit Type"), width=120),
		column("installment_no", _("Installment No"), "Int", 80),
		money("principal", _("Principal")),
		money("interest", _("Interest / Profit")),
		money("total", _("Total")),
		money("paid_principal", _("Paid Principal")),
		money("paid_interest", _("Paid Interest / Profit")),
		money("unpaid_principal", _("Unpaid Principal")),
		money("unpaid_interest", _("Unpaid Interest / Profit")),
		money("unpaid", _("Unpaid")),
		column("status_label", _("Status"), width=100),
		column("status", _("Status"), width=90, hidden=1),
		column("currency", _("Currency"), "Link", 70, options="Currency"),
		money("unpaid_company", _("Unpaid (Company Currency)"), "company_currency", 150),
		column("bank_account", _("Bank Account"), "Link", 160, options="Bank Account"),
		column("company_currency", _("Company Currency", context=CTX), "Link", 80, options="Currency", hidden=1),
	]
