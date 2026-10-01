"""Treasury Dashboard: the Employee Custody section (spec 9 §6.2, design 9 §3), read only.

One query over the company's open Custody Requests the user may read (custody's row permissions apply), five
KPI cards that drill down to custody's own reports and list. Called by `treasury.pdc.overview.get_overview`
only when the custody app is installed and the user may read Custody Request for the company.
"""

import frappe
from frappe.utils import cint, flt

from treasury.integrations.custody import REQUEST

TEMPORARY, PERMANENT = "Temporary", "Permanent"
AWAITING_FUNDING = "Awaiting Funding"
BALANCES = ["query-report", "Custody Balances"]
REGISTER = ["query-report", "Permanent Custody Register"]
REQUESTS = ["List", REQUEST]
TO_PAY_STATUSES = ["Approved", "Partly Paid", AWAITING_FUNDING]
FIELDS = [
	"name",
	"custody_type",
	"status",
	"balance_amount",
	"is_overdue",
	"float_amount",
	"remaining_to_pay",
	"replenishment_amount",
	"replenishment_due",
	"closing_requested",
]


def custody_section(ctx) -> frappe._dict:
	from treasury.pdc.overview import kpi

	rows = frappe.get_list(
		REQUEST, filters={"company": ctx.company, "docstatus": 1, "is_closed": 0}, fields=FIELDS
	)
	company = {"company": ctx.company}

	def balance(r):
		return max(flt(r.balance_amount), 0.0)

	def to_pay(r):
		if r.custody_type == TEMPORARY:
			return flt(r.remaining_to_pay)
		if r.custody_type == PERMANENT and r.status == AWAITING_FUNDING:
			return flt(r.replenishment_amount)
		return 0.0

	open_ = [r for r in rows if flt(r.balance_amount) > 0]
	overdue = [r for r in rows if cint(r.is_overdue)]
	permanent = [r for r in rows if r.custody_type == PERMANENT]
	due = [r for r in permanent if cint(r.replenishment_due) and not cint(r.closing_requested)]
	unpaid = [r for r in rows if to_pay(r) > 0]

	permanent_kpi = kpi(ctx, len(permanent), sum(balance(r) for r in permanent), REGISTER, company)
	permanent_kpi.extra = flt(sum(flt(r.float_amount) for r in permanent), ctx.precision)
	return frappe._dict(
		open=kpi(ctx, len(open_), sum(balance(r) for r in open_), BALANCES, company),
		overdue=kpi(
			ctx, len(overdue), sum(balance(r) for r in overdue), BALANCES, {**company, "only_overdue": 1}
		),
		to_pay=kpi(
			ctx,
			len(unpaid),
			sum(to_pay(r) for r in unpaid),
			REQUESTS,
			{**company, "docstatus": 1, "status": ["in", TO_PAY_STATUSES]},
		),
		permanent=permanent_kpi,
		replenishment_due=kpi(
			ctx,
			len(due),
			sum(flt(r.replenishment_amount) for r in due),
			REGISTER,
			{**company, "status": "Replenishment Due"},
		),
	)
