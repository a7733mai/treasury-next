# Copyright (c) 2026, Ahmed Abdelmageed and contributors
# For license information, please see license.txt

"""Cost of Financing (PLAN.md §8): interest / profit, commission, fees and penalties per facility and period.

Read from the GL (company currency): the expense rows of the Journal Entries linked to a Facility Drawdown or
a Facility Repayment (`treasury_source_*`), dated in [From Date, To Date], cancelled ones excluded. A row is
- Fees: the upfront fees Journal Entry of a drawdown;
- Commission: the facility's Commission Expense Account;
- Penalty: the Penalty Account of a repayment;
- Interest / Profit: every other expense row (the interest accruals and the interest paid by repayments).
Rows on the company's Exchange Gain / Loss account (rounding of foreign currency vouchers) are not a cost of
financing and are left out. Overdraft interest charged on the bank statement is not linked to a facility and
is not included.

Tree: Facility (its totals) → one row per period (Monthly / Quarterly / Yearly).
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import add_months, flt, formatdate, get_last_day, getdate

from treasury.bank_facilities import accounting
from treasury.bank_facilities.report_utils import DRAWDOWN, FACILITY, get_facilities, report_today
from treasury.pdc.report_utils import CTX, column, get_precision, validate_company, validate_date_range

REPAYMENT = "Facility Repayment"
MONTHLY, QUARTERLY, YEARLY = "Monthly", "Quarterly", "Yearly"
PERIOD_MONTHS = {MONTHLY: 1, QUARTERLY: 3, YEARLY: 12}
CATEGORIES = ("interest", "commission", "fees", "penalty")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	company = validate_company(filters.company, FACILITY)
	today = report_today()
	from_date = getdate(filters.from_date or today.replace(month=1, day=1))
	to_date = getdate(filters.to_date or today)
	validate_date_range(from_date, to_date)
	periodicity = filters.periodicity or MONTHLY
	if periodicity not in PERIOD_MONTHS:
		frappe.throw(
			_("Periodicity must be one of: {0}.").format(", ".join(_(p, context=CTX) for p in PERIOD_MONTHS))
		)

	conditions = {"company": company}
	for fieldname, value in (("bank", filters.bank), ("name", filters.facility)):
		if value:
			conditions[fieldname] = value
	facilities = {
		f.name: f
		for f in get_facilities(
			conditions,
			fields=["name", "facility_name", "bank", "currency", "commission_expense_account"],
		)
	}
	costs = get_costs(company, facilities, from_date, to_date)

	precision = get_precision()
	data, totals = [], dict.fromkeys(CATEGORIES, 0.0)
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	for name in sorted(facilities):
		by_period: dict = {}
		for row in costs.get(name, []):
			start = period_start(row.posting_date, periodicity)
			bucket = by_period.setdefault(start, dict.fromkeys(CATEGORIES, 0.0))
			bucket[row.category] += row.amount
		if not by_period:
			continue
		facility = facilities[name]
		parent = frappe._dict(
			row_id=name,
			indent=0,
			label=facility.facility_name,
			facility=name,
			bank=facility.bank,
			currency=company_currency,
			**dict.fromkeys(CATEGORIES, 0.0),
		)
		children = []
		for start in sorted(by_period):
			end = getdate(get_last_day(add_months(start, PERIOD_MONTHS[periodicity] - 1)))
			child = frappe._dict(
				row_id=f"{name}-{start}",
				parent_row_id=name,
				indent=1,
				label=f"{formatdate(start)} - {formatdate(end)}",
				period_start=start,
				period_end=end,
				facility=name,
				bank=facility.bank,
				currency=company_currency,
			)
			for category in CATEGORIES:
				child[category] = flt(by_period[start][category], precision)
				parent[category] = flt(parent[category] + child[category], precision)
			child.total = flt(sum(child[c] for c in CATEGORIES), precision)
			children.append(child)
		parent.total = flt(sum(parent[c] for c in CATEGORIES), precision)
		for category in CATEGORIES:
			totals[category] = flt(totals[category] + parent[category], precision)
		data += [parent, *children]
	return get_columns(), data, None, None, get_summary(totals, company_currency)


def period_start(date, periodicity: str):
	date = getdate(date)
	months = PERIOD_MONTHS[periodicity]
	month = ((date.month - 1) // months) * months + 1
	return date.replace(month=month, day=1)


def get_costs(company: str, facilities: dict, from_date, to_date) -> dict[str, list[frappe._dict]]:
	"""{facility: [{posting_date, category, amount}]} from the GL (see module docstring)."""
	if not facilities:
		return {}
	drawdowns = {
		d.name: d.facility
		for d in frappe.get_all(
			DRAWDOWN,
			filters={"facility": ["in", list(facilities)], "docstatus": ["!=", 0]},
			fields=["name", "facility"],
		)
	}
	if not drawdowns:
		return {}
	repayments = {
		r.name: r
		for r in frappe.get_all(
			REPAYMENT,
			filters={"drawdown": ["in", list(drawdowns)], "docstatus": ["!=", 0]},
			fields=["name", "drawdown", "penalty_account"],
		)
	}
	sources = {(DRAWDOWN, name): facility for name, facility in drawdowns.items()}
	sources.update({(REPAYMENT, name): drawdowns.get(r.drawdown) for name, r in repayments.items()})

	gle = frappe.qb.DocType("GL Entry")
	je = frappe.qb.DocType("Journal Entry")
	account = frappe.qb.DocType("Account")
	query = (
		frappe.qb.from_(gle)
		.join(je)
		.on(je.name == gle.voucher_no)
		.join(account)
		.on(account.name == gle.account)
		.select(
			je.treasury_source_doctype,
			je.treasury_source_name,
			je.treasury_event,
			gle.account,
			gle.posting_date,
			Sum(gle.debit - gle.credit).as_("amount"),
		)
		.where(
			(gle.company == company)
			& (gle.voucher_type == "Journal Entry")
			& (gle.is_cancelled == 0)
			& (account.root_type == "Expense")
			& (gle.posting_date >= getdate(from_date))
			& (gle.posting_date <= getdate(to_date))
			& (je.treasury_source_doctype.isin([DRAWDOWN, REPAYMENT]))
			& (je.treasury_source_name.isin(sorted({s[1] for s in sources})))
		)
		.groupby(
			je.treasury_source_doctype,
			je.treasury_source_name,
			je.treasury_event,
			gle.account,
			gle.posting_date,
		)
	)
	exchange_account = frappe.get_cached_value("Company", company, "exchange_gain_loss_account")
	costs: dict[str, list] = {}
	for row in query.run(as_dict=True):
		facility_name = sources.get((row.treasury_source_doctype, row.treasury_source_name))
		if not facility_name or row.account == exchange_account or not flt(row.amount):
			continue
		facility = facilities[facility_name]
		if row.treasury_event == accounting.EVENT_FEES:
			category = "fees"
		elif facility.commission_expense_account and row.account == facility.commission_expense_account:
			category = "commission"
		elif (
			row.treasury_source_doctype == REPAYMENT
			and row.account == repayments[row.treasury_source_name].penalty_account
		):
			category = "penalty"
		else:
			category = "interest"
		costs.setdefault(facility_name, []).append(
			frappe._dict(posting_date=getdate(row.posting_date), category=category, amount=flt(row.amount))
		)
	return costs


def get_summary(totals: dict, currency: str) -> list[dict]:
	precision = get_precision()

	def card(value, label, indicator):
		return {
			"label": label,
			"value": flt(value, precision),
			"datatype": "Currency",
			"currency": currency,
			"indicator": indicator,
		}

	return [
		card(totals["interest"], _("Interest / Profit"), "Red"),
		card(totals["commission"], _("Commission"), "Orange"),
		card(totals["fees"], _("Fees"), "Orange"),
		card(totals["penalty"], _("Penalties"), "Red"),
		card(sum(totals.values()), _("Total Cost of Financing"), "Blue"),
	]


def get_columns() -> list[dict]:
	def money(fieldname, label, width=130):
		return column(fieldname, label, "Currency", width, options="currency")

	return [
		column("label", _("Facility / Period"), width=240),
		column("facility", _("Bank Facility"), "Link", 150, options=FACILITY),
		column("bank", _("Bank"), "Link", 130, options="Bank"),
		money("interest", _("Interest / Profit")),
		money("commission", _("Commission")),
		money("fees", _("Fees")),
		money("penalty", _("Penalties")),
		money("total", _("Total")),
		column("currency", _("Currency"), "Link", 70, options="Currency", hidden=1),
	]
